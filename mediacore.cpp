// mediacore.cpp
// 性能关键部分（视频/音频解码）用 C++ 实现，导出 extern "C" 接口，
// 由 Python 通过 ctypes 调用。链接 sdk/ffmpeg 提供的 .a 导入库。
//
// 接口概览：
//   视频缩略图：mc_thumb（解码前 N 帧，取首帧作缩略图）
//   视频流式播放：mc_open_video_stream / mc_stream_* 系列（后台线程解码 + 帧缓存 + seek）
//   音频：mc_open_audio（解码为 s16 交织 PCM）/ get_info / copy / close
//
// 流式解码说明：
//   - mc_open_video_stream 打开文件并启动一个后台解码线程，按帧顺序解码为 RGB24 存入有界缓存。
//   - Python 端用定时器按当前帧号调用 mc_stream_get_frame 取帧显示，实现“边解码边播放”。
//   - ffmpeg 解码器开启多线程(thread_count=cpu 数)，繁重的解码工作在 C++ 线程内完成。
//   - max_cache 限制缓存帧数，避免长视频占用过多内存。

#include <cstring>
#include <vector>
#include <string>
#include <cstdint>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <map>
#include <atomic>

extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libswscale/swscale.h>
#include <libswresample/swresample.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libavutil/cpu.h>
}

// ---------------------------------------------------------------------------
// 缩略图（解码前 N 帧，取首帧）
// ---------------------------------------------------------------------------
static int decode_video_thumb(const char* path, int max_w, int max_h,
                              std::vector<uint8_t>* out_rgb,
                              int limit, int* w, int* h, double* fps) {
    AVFormatContext* fmt = nullptr;
    if (avformat_open_input(&fmt, path, nullptr, nullptr) < 0) return -1;
    if (avformat_find_stream_info(fmt, nullptr) < 0) { avformat_close_input(&fmt); return -1; }

    int vidx = av_find_best_stream(fmt, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
    if (vidx < 0) { avformat_close_input(&fmt); return -1; }

    AVCodecParameters* par = fmt->streams[vidx]->codecpar;
    const AVCodec* codec = avcodec_find_decoder(par->codec_id);
    if (!codec) { avformat_close_input(&fmt); return -1; }

    AVCodecContext* cc = avcodec_alloc_context3(codec);
    if (!cc) { avformat_close_input(&fmt); return -1; }
    if (avcodec_parameters_to_context(cc, par) < 0) { avcodec_free_context(&cc); avformat_close_input(&fmt); return -1; }
    cc->thread_count = av_cpu_count();
    if (avcodec_open2(cc, codec, nullptr) < 0) { avcodec_free_context(&cc); avformat_close_input(&fmt); return -1; }

    AVRational fr = fmt->streams[vidx]->avg_frame_rate;
    double f = (fr.num && fr.den) ? (double)fr.num / fr.den : 25.0;

    int sw = par->width, sh = par->height;
    double scale = (std::min)((double)max_w / sw, (double)max_h / sh);
    if (scale > 1) scale = 1;
    int dw = (int)(sw * scale); if (dw < 2) dw = 2;
    int dh = (int)(sh * scale); if (dh < 2) dh = 2;
    if (dw % 2) dw++;
    if (dh % 2) dh++;

    SwsContext* sws = sws_getContext(sw, sh, cc->pix_fmt, dw, dh, AV_PIX_FMT_RGB24,
                                     SWS_BILINEAR, nullptr, nullptr, nullptr);
    if (!sws) { avcodec_free_context(&cc); avformat_close_input(&fmt); return -1; }

    AVFrame* frame = av_frame_alloc();
    AVPacket* pkt = av_packet_alloc();
    std::vector<uint8_t> rgb((size_t)dw * dh * 3);
    int decoded = 0;
    bool stop = false;

    while (!stop && av_read_frame(fmt, pkt) >= 0) {
        if (pkt->stream_index == vidx) {
            avcodec_send_packet(cc, pkt);
            while (avcodec_receive_frame(cc, frame) >= 0) {
                uint8_t* dst[1] = { rgb.data() };
                int dstStride[1] = { dw * 3 };
                sws_scale(sws, (const uint8_t* const*)frame->data, frame->linesize,
                          0, sh, dst, dstStride);
                if (out_rgb) *out_rgb = rgb;
                decoded++;
                if (limit > 0 && decoded >= limit) { stop = true; break; }
            }
        }
        av_packet_unref(pkt);
        if (stop) break;
    }

    *w = dw; *h = dh; *fps = f;

    av_frame_free(&frame);
    av_packet_free(&pkt);
    sws_freeContext(sws);
    avcodec_free_context(&cc);
    avformat_close_input(&fmt);
    return decoded;
}

extern "C" int mc_thumb(const char* path, int max_w, int max_h,
                        unsigned char* out, int out_size,
                        int* w, int* h, double* fps, int max_frames) {
    std::vector<uint8_t> rgb;
    int r = decode_video_thumb(path, max_w, max_h, &rgb, max_frames > 0 ? max_frames : 1,
                               w, h, fps);
    if (r < 0 || rgb.empty()) return -1;
    int need = (*w) * (*h) * 3;
    if (out_size < need) return -2;
    memcpy(out, rgb.data(), need);
    return (int)rgb.size() / ((*w) * (*h) * 3);
}

// ---------------------------------------------------------------------------
// 流式视频解码（后台线程 + 有界帧缓存 + seek）
// ---------------------------------------------------------------------------
struct StreamCtx {
    AVFormatContext* fmt = nullptr;
    AVCodecContext* cc = nullptr;
    SwsContext* sws = nullptr;
    int vidx = -1;
    int sw = 0, sh = 0, dw = 0, dh = 0;
    double fps = 25.0;
    int frame_bytes = 0;
    int64_t total_est = 0;

    std::thread worker;
    std::mutex mtx;
    std::condition_variable cv;
    std::map<int, std::vector<uint8_t>> cache;   // 帧号 -> RGB
    int next_index = 0;        // 下一个待解码帧号
    int seek_request = -1;     // -1 表示无；否则跳转到该帧号
    bool stop_flag = false;
    bool eof_flag = false;
    int max_cache = 240;       // 有界缓存，防止长视频占满内存
};

static void stream_worker(StreamCtx* s) {
    AVPacket* pkt = av_packet_alloc();
    AVFrame* frame = av_frame_alloc();
    std::vector<uint8_t> rgb((size_t)s->dw * s->dh * 3);

    while (!s->stop_flag) {
        // 处理 seek 请求
        int do_seek = -1;
        {
            std::lock_guard<std::mutex> lk(s->mtx);
            if (s->seek_request != -1) { do_seek = s->seek_request; s->seek_request = -1; }
        }
        if (do_seek != -1) {
            s->eof_flag = false;
            avcodec_flush_buffers(s->cc);
            int64_t ts = (int64_t)(do_seek / s->fps *
                                   av_q2d(s->fmt->streams[s->vidx]->time_base));
            av_seek_frame(s->fmt, s->vidx, ts, AVSEEK_FLAG_BACKWARD);
            std::lock_guard<std::mutex> lk(s->mtx);
            s->cache.clear();
            s->next_index = do_seek;
        }

        int r = av_read_frame(s->fmt, pkt);
        if (r < 0) {
            std::lock_guard<std::mutex> lk(s->mtx);
            s->eof_flag = true;
            s->cv.notify_all();
            break;
        }

        if (pkt->stream_index == s->vidx) {
            avcodec_send_packet(s->cc, pkt);
            while (avcodec_receive_frame(s->cc, frame) >= 0) {
                uint8_t* dst[1] = { rgb.data() };
                int dstStride[1] = { s->dw * 3 };
                sws_scale(s->sws, (const uint8_t* const*)frame->data, frame->linesize,
                          0, s->sh, dst, dstStride);
                {
                    std::lock_guard<std::mutex> lk(s->mtx);
                    if ((int)s->cache.size() >= s->max_cache && !s->cache.empty())
                        s->cache.erase(s->cache.begin());
                    s->cache[s->next_index++] = rgb;
                }
                s->cv.notify_all();
            }
        }
        av_packet_unref(pkt);
    }

    av_frame_free(&frame);
    av_packet_free(&pkt);
}

extern "C" void* mc_open_video_stream(const char* path, int max_w, int max_h) {
    StreamCtx* s = new StreamCtx();
    if (avformat_open_input(&s->fmt, path, nullptr, nullptr) < 0) { delete s; return nullptr; }
    if (avformat_find_stream_info(s->fmt, nullptr) < 0) { avformat_close_input(&s->fmt); delete s; return nullptr; }

    s->vidx = av_find_best_stream(s->fmt, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
    if (s->vidx < 0) { avformat_close_input(&s->fmt); delete s; return nullptr; }

    AVCodecParameters* par = s->fmt->streams[s->vidx]->codecpar;
    const AVCodec* codec = avcodec_find_decoder(par->codec_id);
    if (!codec) { avformat_close_input(&s->fmt); delete s; return nullptr; }

    s->cc = avcodec_alloc_context3(codec);
    if (!s->cc) { avformat_close_input(&s->fmt); delete s; return nullptr; }
    if (avcodec_parameters_to_context(s->cc, par) < 0) { avcodec_free_context(&s->cc); avformat_close_input(&s->fmt); delete s; return nullptr; }
    s->cc->thread_count = av_cpu_count();   // ffmpeg 内部多线程解码
    if (avcodec_open2(s->cc, codec, nullptr) < 0) { avcodec_free_context(&s->cc); avformat_close_input(&s->fmt); delete s; return nullptr; }

    s->sw = par->width; s->sh = par->height;
    double scale = (std::min)((double)max_w / s->sw, (double)max_h / s->sh);
    if (scale > 1) scale = 1;
    s->dw = (int)(s->sw * scale); if (s->dw < 2) s->dw = 2;
    s->dh = (int)(s->sh * scale); if (s->dh < 2) s->dh = 2;
    if (s->dw % 2) s->dw++;
    if (s->dh % 2) s->dh++;

    s->sws = sws_getContext(s->sw, s->sh, s->cc->pix_fmt, s->dw, s->dh, AV_PIX_FMT_RGB24,
                            SWS_BILINEAR, nullptr, nullptr, nullptr);
    if (!s->sws) { avcodec_free_context(&s->cc); avformat_close_input(&s->fmt); delete s; return nullptr; }

    AVRational fr = s->fmt->streams[s->vidx]->avg_frame_rate;
    s->fps = (fr.num && fr.den) ? (double)fr.num / fr.den : 25.0;
    double dur = s->fmt->duration / (double)AV_TIME_BASE;
    s->total_est = (int)(dur * s->fps);
    if (s->total_est <= 0) s->total_est = 1;
    s->frame_bytes = s->dw * s->dh * 3;

    s->worker = std::thread(stream_worker, s);
    return s;
}

extern "C" int mc_stream_get_info(void* ctx, int* w, int* h, double* fps, int* count) {
    StreamCtx* s = (StreamCtx*)ctx;
    if (!s) return -1;
    *w = s->dw; *h = s->dh; *fps = s->fps; *count = (int)s->total_est;
    return 0;
}

// 取帧：返回 1 成功(已写入 out)、0 尚未解码、 -2 超出已解码范围(EOF)、 -1 参数错误、 -3 缓冲区不足
extern "C" int mc_stream_get_frame(void* ctx, int index, unsigned char* out, int out_size,
                                    int* w, int* h) {
    StreamCtx* s = (StreamCtx*)ctx;
    if (!s || index < 0) return -1;
    std::lock_guard<std::mutex> lk(s->mtx);
    if (s->eof_flag && index >= s->next_index) return -2;
    auto it = s->cache.find(index);
    if (it == s->cache.end()) return 0;
    int need = s->frame_bytes;
    if (out_size < need) return -3;
    memcpy(out, it->second.data(), need);
    *w = s->dw; *h = s->dh;
    return 1;
}

extern "C" int mc_stream_decoded_count(void* ctx) {
    StreamCtx* s = (StreamCtx*)ctx;
    if (!s) return 0;
    std::lock_guard<std::mutex> lk(s->mtx);
    return s->next_index;
}

extern "C" void mc_stream_seek(void* ctx, int index) {
    StreamCtx* s = (StreamCtx*)ctx;
    if (!s || index < 0) return;
    std::lock_guard<std::mutex> lk(s->mtx);
    s->seek_request = index;
}

extern "C" void mc_stream_close(void* ctx) {
    StreamCtx* s = (StreamCtx*)ctx;
    if (!s) return;
    {
        std::lock_guard<std::mutex> lk(s->mtx);
        s->stop_flag = true;
    }
    s->cv.notify_all();
    if (s->worker.joinable()) s->worker.join();
    if (s->sws) sws_freeContext(s->sws);
    if (s->cc) avcodec_free_context(&s->cc);
    if (s->fmt) avformat_close_input(&s->fmt);
    delete s;
}

// ---------------------------------------------------------------------------
// 音频解码（ffmpeg -> s16 交织 PCM）
// ---------------------------------------------------------------------------
struct AudioCtx {
    std::vector<uint8_t> pcm;
    int sample_rate = 0;
    int channels = 0;
    int64_t total_samples = 0;
};

static int decode_audio(const char* path, AudioCtx* out) {
    AVFormatContext* fmt = nullptr;
    if (avformat_open_input(&fmt, path, nullptr, nullptr) < 0) return -1;
    if (avformat_find_stream_info(fmt, nullptr) < 0) { avformat_close_input(&fmt); return -1; }

    int aidx = av_find_best_stream(fmt, AVMEDIA_TYPE_AUDIO, -1, -1, nullptr, 0);
    if (aidx < 0) { avformat_close_input(&fmt); return -1; }

    AVCodecParameters* par = fmt->streams[aidx]->codecpar;
    const AVCodec* codec = avcodec_find_decoder(par->codec_id);
    if (!codec) { avformat_close_input(&fmt); return -1; }

    AVCodecContext* cc = avcodec_alloc_context3(codec);
    if (!cc) { avformat_close_input(&fmt); return -1; }
    if (avcodec_parameters_to_context(cc, par) < 0) { avcodec_free_context(&cc); avformat_close_input(&fmt); return -1; }
    if (avcodec_open2(cc, codec, nullptr) < 0) { avcodec_free_context(&cc); avformat_close_input(&fmt); return -1; }

    int channels = cc->ch_layout.nb_channels;
    int rate = cc->sample_rate;

    AVChannelLayout in_ch, out_ch;
    av_channel_layout_default(&in_ch, channels);
    av_channel_layout_default(&out_ch, channels);

    SwrContext* swr = nullptr;
    if (swr_alloc_set_opts2(&swr, &out_ch, AV_SAMPLE_FMT_S16, rate,
                            &in_ch, cc->sample_fmt, rate, 0, nullptr) < 0) {
        swr_free(&swr); avcodec_free_context(&cc); avformat_close_input(&fmt); return -1;
    }
    if (swr_init(swr) < 0) { swr_free(&swr); avcodec_free_context(&cc); avformat_close_input(&fmt); return -1; }

    AVFrame* frame = av_frame_alloc();
    AVPacket* pkt = av_packet_alloc();
    int64_t total = 0;

    while (av_read_frame(fmt, pkt) >= 0) {
        if (pkt->stream_index == aidx) {
            avcodec_send_packet(cc, pkt);
            while (avcodec_receive_frame(cc, frame) >= 0) {
                uint8_t* out_data = nullptr;
                int out_linesize = 0;
                int ret = av_samples_alloc(&out_data, &out_linesize, channels,
                                           frame->nb_samples, AV_SAMPLE_FMT_S16, 0);
                if (ret < 0) { av_frame_unref(frame); continue; }
                int conv = swr_convert(swr, &out_data, frame->nb_samples,
                                       (const uint8_t**)frame->data, frame->nb_samples);
                if (conv > 0) {
                    int bytes = conv * channels * 2;
                    out->pcm.insert(out->pcm.end(), out_data, out_data + bytes);
                    total += conv;
                }
                av_freep(&out_data);
            }
        }
        av_packet_unref(pkt);
    }

    out->sample_rate = rate;
    out->channels = channels;
    out->total_samples = total;

    av_frame_free(&frame);
    av_packet_free(&pkt);
    swr_free(&swr);
    avcodec_free_context(&cc);
    avformat_close_input(&fmt);
    return 0;
}

extern "C" void* mc_open_audio(const char* path) {
    AudioCtx* ctx = new AudioCtx();
    if (decode_audio(path, ctx) < 0) { delete ctx; return nullptr; }
    if (ctx->pcm.empty()) { delete ctx; return nullptr; }
    return ctx;
}

extern "C" int mc_audio_get_info(void* ctx, int* sample_rate, int* channels, int64_t* total_samples) {
    AudioCtx* c = (AudioCtx*)ctx;
    if (!c) return -1;
    *sample_rate = c->sample_rate;
    *channels = c->channels;
    *total_samples = c->total_samples;
    return 0;
}

extern "C" int mc_audio_copy(void* ctx, int64_t start_sample, int num_samples,
                             unsigned char* out, int out_size) {
    AudioCtx* c = (AudioCtx*)ctx;
    if (!c || start_sample < 0) return -1;
    int64_t start_byte = start_sample * c->channels * 2;
    int64_t avail = (int64_t)c->pcm.size() - start_byte;
    if (avail <= 0) return 0;
    int want = num_samples * c->channels * 2;
    int n = (int)((want < avail && want > 0) ? want : avail);
    if (n > out_size) n = out_size;
    memcpy(out, c->pcm.data() + start_byte, n);
    return n;
}

extern "C" void mc_close_audio(void* ctx) {
    AudioCtx* c = (AudioCtx*)ctx;
    if (c) delete c;
}
