#include "media_extractor.h"

#include <QCoreApplication>
#include <QDataStream>
#include <QDir>
#include <QFileInfo>
#include <QImage>
#include <QLibrary>
#include <QSaveFile>
#include <QSet>
#include <QTransform>
#include <QUuid>
#include <QtEndian>

#include <algorithm>
#include <bit>
#include <cmath>
#include <limits>
#include <memory>
#include <stdexcept>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/channel_layout.h>
#include <libavutil/display.h>
#include <libavutil/error.h>
#include <libavutil/frame.h>
#include <libavutil/version.h>
#include <libswresample/swresample.h>
#include <libswscale/swscale.h>
}

namespace qcopilots_search {
namespace {

constexpr int sampleRate = 16000;
constexpr int windowSamples = sampleRate * 30;
constexpr int hopSamples = sampleRate * 25;
constexpr int minimumTailSamples = sampleRate * 2;
constexpr int maximumFrames = 1200;
constexpr int maximumFrameBatch = 70;

class MediaFailure final : public std::runtime_error {
public:
    explicit MediaFailure(const QString &message) : std::runtime_error(message.toUtf8().constData()) {}
};

// Function pointer types come exclusively from the locked public FFmpeg headers.
// Never substitute private struct declarations or resolve a library by PATH.
struct FFmpeg {
    // Keep the root dependency group alive until the other handles are gone.
    // On Linux its SSL stub also supplies symbols used by avutil.
    QLibrary format;
    QLibrary util, resample, scale, codec;
    QString error;
#define API(name) decltype(&::name) name = nullptr
    API(avutil_version); API(swresample_version); API(swscale_version);
    API(avcodec_version); API(avformat_version);
    API(av_strerror); API(av_dict_set); API(av_dict_free);
    API(av_frame_alloc); API(av_frame_free); API(av_frame_unref); API(av_frame_get_side_data);
    API(av_display_rotation_get); API(av_channel_layout_default); API(av_channel_layout_uninit);
    API(av_packet_alloc); API(av_packet_free); API(av_packet_unref);
    API(avformat_alloc_context); API(avformat_free_context); API(avformat_open_input);
    API(avformat_find_stream_info); API(avformat_close_input); API(av_find_best_stream);
    API(av_read_frame); API(av_guess_frame_rate);
    API(avcodec_alloc_context3); API(avcodec_parameters_to_context); API(avcodec_open2);
    API(avcodec_free_context); API(avcodec_send_packet); API(avcodec_receive_frame);
    API(swr_alloc_set_opts2); API(swr_init); API(swr_free); API(swr_convert); API(swr_get_out_samples);
    API(sws_getContext); API(sws_scale); API(sws_freeContext);
#undef API

    static QString fileName(const QString &base, int major) {
#if defined(Q_OS_WIN)
        return QStringLiteral("%1-%2.dll").arg(base).arg(major);
#elif defined(Q_OS_MACOS)
        return QStringLiteral("lib%1.%2.dylib").arg(base).arg(major);
#else
        return QStringLiteral("lib%1.so.%2").arg(base).arg(major);
#endif
    }

    template<class Function> void resolve(QLibrary &library, Function &function, const char *name) {
        function = reinterpret_cast<Function>(library.resolve(name));
        if (!function) throw MediaFailure(QStringLiteral("FFmpeg symbol missing: %1 (%2)")
                                             .arg(QString::fromLatin1(name), library.errorString()));
    }

    void loadLibrary(QLibrary &library, const QDir &directory, const QString &base, int major) {
        const QString path = directory.absoluteFilePath(fileName(base, major));
        if (!QFileInfo(path).isFile()) throw MediaFailure(QStringLiteral("Missing locked FFmpeg library: %1").arg(path));
        library.setFileName(path);
#if defined(Q_OS_LINUX)
        // Qt's FFmpeg stubs must stay local to this dependency group instead
        // of interposing on the application's OpenSSL and VAAPI symbols.
        library.setLoadHints(QLibrary::ResolveAllSymbolsHint);
#else
        library.setLoadHints(QLibrary::ResolveAllSymbolsHint | QLibrary::ExportExternalSymbolsHint);
#endif
        if (!library.load()) throw MediaFailure(QStringLiteral("Cannot load %1: %2").arg(path, library.errorString()));
    }

    FFmpeg() {
        try {
            QStringList locations{QCoreApplication::applicationDirPath(),
                QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../lib"))};
#if defined(Q_OS_MACOS)
            locations << QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../Frameworks"));
#endif
#ifdef QCOPILOTS_SEARCH_FFMPEG_RUNTIME_DIR
            locations << QString::fromUtf8(QCOPILOTS_SEARCH_FFMPEG_RUNTIME_DIR);
#endif
            QDir directory;
            bool found = false;
            for (const auto &location : locations) {
                const QDir candidate(location);
                if (QFileInfo(candidate.filePath(fileName(QStringLiteral("avformat"), LIBAVFORMAT_VERSION_MAJOR))).isFile()) {
                    directory = candidate;
                    found = true;
                    break;
                }
            }
            if (!found) throw MediaFailure(QStringLiteral("Locked FFmpeg runtime was not found beside the application or in the configured Qt SDK"));
#if defined(Q_OS_LINUX)
            // Qt's avutil needs RAND_bytes, provided by the SSL stub in
            // avformat's dependency group rather than its own crypto stub.
            // Load that complete group before eagerly resolving avutil.
            loadLibrary(format, directory, QStringLiteral("avformat"), LIBAVFORMAT_VERSION_MAJOR);
#endif
            loadLibrary(util, directory, QStringLiteral("avutil"), LIBAVUTIL_VERSION_MAJOR);
            loadLibrary(resample, directory, QStringLiteral("swresample"), LIBSWRESAMPLE_VERSION_MAJOR);
            loadLibrary(scale, directory, QStringLiteral("swscale"), LIBSWSCALE_VERSION_MAJOR);
            loadLibrary(codec, directory, QStringLiteral("avcodec"), LIBAVCODEC_VERSION_MAJOR);
#if !defined(Q_OS_LINUX)
            loadLibrary(format, directory, QStringLiteral("avformat"), LIBAVFORMAT_VERSION_MAJOR);
#endif
#define LOAD(library, name) resolve(library, name, #name)
            LOAD(util, avutil_version); LOAD(resample, swresample_version); LOAD(scale, swscale_version);
            LOAD(codec, avcodec_version); LOAD(format, avformat_version);
            if (avutil_version() != LIBAVUTIL_VERSION_INT || swresample_version() != LIBSWRESAMPLE_VERSION_INT ||
                swscale_version() != LIBSWSCALE_VERSION_INT || avcodec_version() != LIBAVCODEC_VERSION_INT ||
                avformat_version() != LIBAVFORMAT_VERSION_INT) {
                throw MediaFailure(QStringLiteral("FFmpeg runtime/header version mismatch. Deploy the locked FFmpeg 9.0.1 runtime"));
            }
            LOAD(util, av_strerror); LOAD(util, av_dict_set); LOAD(util, av_dict_free);
            LOAD(util, av_frame_alloc); LOAD(util, av_frame_free); LOAD(util, av_frame_unref);
            LOAD(util, av_frame_get_side_data); LOAD(util, av_display_rotation_get);
            LOAD(util, av_channel_layout_default); LOAD(util, av_channel_layout_uninit);
            LOAD(codec, av_packet_alloc); LOAD(codec, av_packet_free); LOAD(codec, av_packet_unref);
            LOAD(format, avformat_alloc_context); LOAD(format, avformat_free_context); LOAD(format, avformat_open_input);
            LOAD(format, avformat_find_stream_info); LOAD(format, avformat_close_input); LOAD(format, av_find_best_stream);
            LOAD(format, av_read_frame); LOAD(format, av_guess_frame_rate);
            LOAD(codec, avcodec_alloc_context3); LOAD(codec, avcodec_parameters_to_context);
            LOAD(codec, avcodec_open2); LOAD(codec, avcodec_free_context);
            LOAD(codec, avcodec_send_packet); LOAD(codec, avcodec_receive_frame);
            LOAD(resample, swr_alloc_set_opts2); LOAD(resample, swr_init); LOAD(resample, swr_free);
            LOAD(resample, swr_convert); LOAD(resample, swr_get_out_samples);
            LOAD(scale, sws_getContext); LOAD(scale, sws_scale); LOAD(scale, sws_freeContext);
#undef LOAD
        } catch (const std::exception &failure) { error = QString::fromUtf8(failure.what()); }
    }

    void check(int result, const QString &action) const {
        if (result >= 0) return;
        char message[AV_ERROR_MAX_STRING_SIZE]{};
        av_strerror(result, message, sizeof(message));
        throw MediaFailure(action + QStringLiteral(": ") + QString::fromUtf8(message));
    }
};

FFmpeg &ffmpeg() { static FFmpeg instance; return instance; }

struct Decoder {
    FFmpeg &api;
    const ExtractionOptions &options;
    AVFormatContext *input = nullptr;
    AVCodecContext *codec = nullptr;
    AVPacket *packet = nullptr;
    AVFrame *frame = nullptr;
    int streamIndex = -1;

    Decoder(FFmpeg &api, const ExtractionOptions &options) : api(api), options(options) {}
    ~Decoder() {
        if (frame) api.av_frame_free(&frame);
        if (packet) api.av_packet_free(&packet);
        if (codec) api.avcodec_free_context(&codec);
        if (input) api.avformat_close_input(&input);
    }
    static int interrupt(void *opaque) {
        const auto *options = static_cast<const ExtractionOptions *>(opaque);
        return options->cancelled && options->cancelled() ? 1 : 0;
    }
    bool open(const QString &path, AVMediaType type) {
        input = api.avformat_alloc_context();
        if (!input) throw MediaFailure(QStringLiteral("Cannot allocate media input"));
        input->interrupt_callback = {interrupt, const_cast<ExtractionOptions *>(&options)};
        AVDictionary *settings = nullptr;
        api.av_dict_set(&settings, "protocol_whitelist", "file", 0);
        const auto encoded = path.toUtf8();
        const int opened = api.avformat_open_input(&input, encoded.constData(), nullptr, &settings);
        api.av_dict_free(&settings);
        api.check(opened, QStringLiteral("Open media"));
        api.check(api.avformat_find_stream_info(input, nullptr), QStringLiteral("Inspect media streams"));
        const AVCodec *decoder = nullptr;
        streamIndex = api.av_find_best_stream(input, type, -1, -1, &decoder, 0);
        if (streamIndex == AVERROR_STREAM_NOT_FOUND) return false;
        api.check(streamIndex, QStringLiteral("Select media decoder"));
        codec = api.avcodec_alloc_context3(decoder);
        if (!codec) throw MediaFailure(QStringLiteral("Cannot allocate media decoder"));
        api.check(api.avcodec_parameters_to_context(codec, input->streams[streamIndex]->codecpar), QStringLiteral("Configure media decoder"));
        codec->thread_count = 2;
        codec->max_pixels = 64LL * 1024 * 1024;
        api.check(api.avcodec_open2(codec, decoder, nullptr), QStringLiteral("Open media decoder"));
        packet = api.av_packet_alloc();
        frame = api.av_frame_alloc();
        if (!packet || !frame) throw MediaFailure(QStringLiteral("Cannot allocate media frame"));
        return true;
    }

    bool cancelled() const { return options.cancelled && options.cancelled(); }

    // The callback returns false after the requested bounded batch is full.
    bool decode(const std::function<bool(AVFrame *)> &consume) {
        const auto drain = [&]() {
            for (;;) {
                if (cancelled()) return false;
                const int result = api.avcodec_receive_frame(codec, frame);
                if (result == AVERROR(EAGAIN) || result == AVERROR_EOF) return true;
                api.check(result, QStringLiteral("Decode media frame"));
                const bool proceed = consume(frame);
                api.av_frame_unref(frame);
                if (!proceed) return false;
            }
        };
        int readResult = 0;
        while (!cancelled() && (readResult = api.av_read_frame(input, packet)) >= 0) {
            if (packet->stream_index == streamIndex) {
                const int sent = api.avcodec_send_packet(codec, packet);
                api.av_packet_unref(packet);
                api.check(sent, QStringLiteral("Send media packet"));
                if (!drain()) return false;
            } else { api.av_packet_unref(packet); }
        }
        if (cancelled()) return false;
        if (readResult != AVERROR_EOF) api.check(readResult, QStringLiteral("Read media packet"));
        api.check(api.avcodec_send_packet(codec, nullptr), QStringLiteral("Flush media decoder"));
        return drain();
    }

    qint64 durationMs() const {
        if (input->duration != AV_NOPTS_VALUE && input->duration > 0) return input->duration / (AV_TIME_BASE / 1000);
        const auto *stream = input->streams[streamIndex];
        if (stream->duration != AV_NOPTS_VALUE && stream->duration > 0)
            return qint64(double(stream->duration) * av_q2d(stream->time_base) * 1000);
        return 0;
    }
};

struct Collector {
    ExtractionResult &result;
    const ExtractionOptions &options;
    QString path;
    QString kind;
    int count = 0;
    int budget = 0;

    bool accepts() const { return count >= options.start; }
    bool full() const { return result.segments.size() >= budget; }
    bool append(Segment piece, const std::function<void(const QString &)> &write, const QString &extension) {
        if (count++ < options.start) return true;
        if (full()) { result.complete = false; return false; }
        piece.path = path;
        piece.title = QFileInfo(path).fileName();
        piece.kind = kind;
        piece.mediaPath = QDir(options.tempDir).filePath(QStringLiteral("qcopilots-search-media-%1.%2")
            .arg(QUuid::createUuid().toString(QUuid::WithoutBraces), extension));
        write(piece.mediaPath);
        result.segments.append(std::move(piece));
        result.next = options.start + result.segments.size();
        return true;
    }
};

void writeWave(const QString &path, const qint16 *samples, int count) {
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly)) throw MediaFailure(file.errorString());
    QDataStream output(&file);
    output.setByteOrder(QDataStream::LittleEndian);
    output.writeRawData("RIFF", 4); output << quint32(36 + count * 2);
    output.writeRawData("WAVEfmt ", 8); output << quint32(16) << quint16(1) << quint16(1)
        << quint32(sampleRate) << quint32(sampleRate * 2) << quint16(2) << quint16(16);
    output.writeRawData("data", 4); output << quint32(count * 2);
    QByteArray bytes(count * 2, Qt::Uninitialized);
    qToLittleEndian<qint16>(samples, count, bytes.data());
    if (output.writeRawData(bytes.constData(), bytes.size()) != bytes.size() || output.status() != QDataStream::Ok || !file.commit())
        throw MediaFailure(QStringLiteral("Cannot write audio window: %1").arg(file.errorString()));
}

bool extractAudio(FFmpeg &api, const QString &path, Collector &collector) {
    Decoder decoder(api, collector.options);
    if (!decoder.open(path, AVMEDIA_TYPE_AUDIO)) {
        if (collector.kind == QStringLiteral("audio")) throw MediaFailure(QStringLiteral("Media source contains no audio stream"));
        return true;
    }
    struct Resampler {
        FFmpeg &api;
        SwrContext *context = nullptr;
        ~Resampler() { if (context) api.swr_free(&context); }
    } resampler{api};
    int inputRate = 0;
    int inputFormat = -1;
    int inputChannels = 0;
    std::vector<qint16> buffer;
    buffer.reserve(windowSamples + sampleRate);
    qint64 offset = 0;
    bool emittedWindow = false;
    bool haveTimestamp = false;
    double nextInputTime = 0;
    const AVStream *audioStream = decoder.input->streams[decoder.streamIndex];
    const double timeBase = av_q2d(audioStream->time_base);
    const double timeOrigin = decoder.input->start_time == AV_NOPTS_VALUE ? 0.0
        : double(decoder.input->start_time) / AV_TIME_BASE;
    const auto emitSegment = [&](int count) {
        double sum = 0;
        for (int i = 0; i < count; ++i) { const double value = buffer[i] / 32768.0; sum += value * value; }
        if (count <= 0 || std::sqrt(sum / count) <= 0.003) return true;
        Segment piece;
        piece.modality = QStringLiteral("audio");
        piece.startMs = offset * 1000 / sampleRate;
        piece.endMs = (offset + count) * 1000 / sampleRate;
        return collector.append(piece, [&](const QString &output) { writeWave(output, buffer.data(), count); }, QStringLiteral("wav"));
    };
    const auto append = [&](const qint16 *samples, int count) {
        buffer.insert(buffer.end(), samples, samples + count);
        while (buffer.size() >= windowSamples) {
            if (!emitSegment(windowSamples)) return false;
            buffer.erase(buffer.begin(), buffer.begin() + hopSamples);
            offset += hopSamples;
            emittedWindow = true;
        }
        return true;
    };
    const auto convert = [&](const uint8_t *const *data, int samples) {
        const int capacity = api.swr_get_out_samples(resampler.context, samples);
        api.check(capacity, QStringLiteral("Calculate audio resample capacity"));
        if (capacity > sampleRate * 120) throw MediaFailure(QStringLiteral("Oversized decoded audio frame"));
        std::vector<qint16> converted(std::max(1, capacity));
        uint8_t *output[] = {reinterpret_cast<uint8_t *>(converted.data())};
        const int count = api.swr_convert(resampler.context, output, capacity, data, samples);
        api.check(count, QStringLiteral("Resample audio"));
        return std::pair{append(converted.data(), count), count};
    };
    const auto flushResampler = [&]() {
        if (resampler.context) for (;;) {
            const auto [proceed, count] = convert(nullptr, 0);
            if (!proceed) return false;
            if (!count) break;
        }
        return true;
    };
    const auto finishRegion = [&]() {
        const int alreadyCovered = emittedWindow ? windowSamples - hopSamples : 0;
        return int(buffer.size()) - alreadyCovered < minimumTailSamples || emitSegment(int(buffer.size()));
    };
    const bool finished = decoder.decode([&](AVFrame *frame) {
        if (frame->sample_rate <= 0 || frame->ch_layout.nb_channels <= 0)
            throw MediaFailure(QStringLiteral("Audio frame has no sample rate or channel layout"));
        const int64_t timestamp = frame->best_effort_timestamp != AV_NOPTS_VALUE ? frame->best_effort_timestamp : frame->pts;
        if (timestamp != AV_NOPTS_VALUE) {
            const double time = timestamp * timeBase - timeOrigin;
            if (!haveTimestamp || std::abs(time - nextInputTime) > 0.050) {
                // A delayed track or a timestamp discontinuity starts a separate
                // timeline region. Never concatenate across a gap and report an
                // inaccurate preview position, or allocate the gap as silence.
                if (haveTimestamp) {
                    if (!flushResampler() || !finishRegion()) return false;
                    buffer.clear();
                    if (resampler.context) api.check(api.swr_init(resampler.context), QStringLiteral("Reset audio resampler"));
                    emittedWindow = false;
                }
                offset = std::max<qint64>(0, qint64(std::llround(time * sampleRate)));
            }
            haveTimestamp = true;
            nextInputTime = time + double(frame->nb_samples) / frame->sample_rate;
        } else if (haveTimestamp) {
            nextInputTime += double(frame->nb_samples) / frame->sample_rate;
        }
        if (!resampler.context) {
            AVChannelLayout mono{};
            api.av_channel_layout_default(&mono, 1);
            const int configured = api.swr_alloc_set_opts2(&resampler.context, &mono, AV_SAMPLE_FMT_S16, sampleRate,
                &frame->ch_layout, static_cast<AVSampleFormat>(frame->format), frame->sample_rate, 0, nullptr);
            api.av_channel_layout_uninit(&mono);
            api.check(configured, QStringLiteral("Configure audio resampler"));
            api.check(api.swr_init(resampler.context), QStringLiteral("Initialize audio resampler"));
            inputRate = frame->sample_rate;
            inputFormat = frame->format;
            inputChannels = frame->ch_layout.nb_channels;
        } else if (inputRate != frame->sample_rate || inputFormat != frame->format || inputChannels != frame->ch_layout.nb_channels) {
            throw MediaFailure(QStringLiteral("Audio format changes within a stream are unsupported"));
        }
        return convert(const_cast<const uint8_t **>(frame->extended_data), frame->nb_samples).first;
    });
    if (!finished) return false;
    return flushResampler() && finishRegion();
}

quint64 averageHash(const QImage &image) {
    const QImage small = image.scaled(8, 8, Qt::IgnoreAspectRatio, Qt::SmoothTransformation).convertToFormat(QImage::Format_Grayscale8);
    int total = 0;
    for (int y = 0; y < 8; ++y) for (int x = 0; x < 8; ++x) total += small.constScanLine(y)[x];
    const int mean = total / 64;
    quint64 hash = 0;
    for (int y = 0; y < 8; ++y) for (int x = 0; x < 8; ++x)
        if (small.constScanLine(y)[x] > mean) hash |= quint64(1) << (y * 8 + x);
    return hash;
}

QImage frameImage(FFmpeg &api, AVFrame *frame) {
    if (frame->width <= 0 || frame->height <= 0 || qint64(frame->width) * frame->height > 64LL * 1024 * 1024)
        throw MediaFailure(QStringLiteral("Invalid or oversized video frame"));
    const double ratio = std::min(1.0, 1024.0 / std::max(frame->width, frame->height));
    const int width = std::max(1, int(frame->width * ratio));
    const int height = std::max(1, int(frame->height * ratio));
    QImage image(width, height, QImage::Format_RGB888);
    if (image.isNull()) throw MediaFailure(QStringLiteral("Cannot allocate video thumbnail"));
    SwsContext *context = api.sws_getContext(frame->width, frame->height, static_cast<AVPixelFormat>(frame->format),
        width, height, AV_PIX_FMT_RGB24, SWS_BILINEAR, nullptr, nullptr, nullptr);
    if (!context) throw MediaFailure(QStringLiteral("Unsupported decoded video pixel format"));
    uint8_t *output[4] = {image.bits(), nullptr, nullptr, nullptr};
    int stride[4] = {int(image.bytesPerLine()), 0, 0, 0};
    const int rows = api.sws_scale(context, frame->data, frame->linesize, 0, frame->height, output, stride);
    api.sws_freeContext(context);
    if (rows != height) throw MediaFailure(QStringLiteral("Incomplete video pixel conversion"));
    if (const auto *rotation = api.av_frame_get_side_data(frame, AV_FRAME_DATA_DISPLAYMATRIX)) {
        if (rotation->size >= 9 * sizeof(int32_t)) {
            const double angle = api.av_display_rotation_get(reinterpret_cast<const int32_t *>(rotation->data));
            if (std::isfinite(angle) && std::abs(angle) > 0.01) image = image.transformed(QTransform().rotate(-angle));
        }
    }
    return image;
}

bool extractVideo(FFmpeg &api, const QString &path, Collector &collector) {
    Decoder decoder(api, collector.options);
    if (!decoder.open(path, AVMEDIA_TYPE_VIDEO)) return true;
    const qint64 duration = decoder.durationMs();
    const qint64 step = std::max<qint64>(3000, (duration + maximumFrames - 1) / maximumFrames);
    qint64 nextTime = duration > 0 ? std::min<qint64>(500, duration / 2) : 500;
    qint64 lastTime = 0;
    const AVStream *stream = decoder.input->streams[decoder.streamIndex];
    const double timeBase = av_q2d(stream->time_base);
    const double timeOrigin = decoder.input->start_time == AV_NOPTS_VALUE ? 0.0
        : double(decoder.input->start_time) / AV_TIME_BASE;
    const AVRational rate = api.av_guess_frame_rate(decoder.input, const_cast<AVStream *>(stream), nullptr);
    const double fallbackStep = rate.num > 0 && rate.den > 0 ? 1000.0 / av_q2d(rate) : 40.0;
    qint64 decodedFrames = 0;
    int sampledFrames = 0;
    QImage pending;
    QImage firstFrame;
    quint64 pendingHash = 0;
    qint64 pendingStart = 0;
    const auto emitSegment = [&](qint64 end) {
        if (pending.isNull()) return true;
        Segment piece;
        piece.modality = QStringLiteral("image");
        piece.imageBudget = 70;
        piece.startMs = pendingStart;
        piece.endMs = std::max(pendingStart + 1, end);
        return collector.append(piece, [&](const QString &output) {
            QSaveFile file(output);
            if (!file.open(QIODevice::WriteOnly) || !pending.save(&file, "PNG") || !file.commit())
                throw MediaFailure(QStringLiteral("Cannot write video frame: %1").arg(file.errorString()));
        }, QStringLiteral("png"));
    };
    const bool finished = decoder.decode([&](AVFrame *frame) {
        const int64_t timestamp = frame->best_effort_timestamp;
        const qint64 milliseconds = timestamp == AV_NOPTS_VALUE ? qint64(decodedFrames * fallbackStep)
            : std::max<qint64>(0, qint64((timestamp * timeBase - timeOrigin) * 1000.0));
        ++decodedFrames;
        lastTime = std::max(lastTime, milliseconds);
        if (decodedFrames == 1 && milliseconds < nextTime) firstFrame = frameImage(api, frame);
        if (milliseconds < nextTime) return true;
        if (sampledFrames >= maximumFrames)
            throw MediaFailure(QStringLiteral("Video exceeds the 1200-sample limit because its duration is missing or inaccurate"));
        ++sampledFrames;
        while (nextTime <= milliseconds) nextTime += step;
        QImage image = frameImage(api, frame);
        const quint64 hash = averageHash(image);
        if (!pending.isNull() && std::popcount(hash ^ pendingHash) <= 4) return true;
        if (!emitSegment(milliseconds)) return false;
        pending = std::move(image);
        pendingHash = hash;
        pendingStart = milliseconds;
        return true;
    });
    if (!finished) return false;
    if (!collector.result.complete || decoder.cancelled()) return false;
    if (pending.isNull() && !firstFrame.isNull()) pending = std::move(firstFrame);
    return emitSegment(std::max(duration, lastTime + qint64(std::ceil(fallbackStep))));
}

QString extension(const QString &path) {
    const QString suffix = QFileInfo(path).suffix().toLower();
    return suffix.isEmpty() ? path.toLower() : suffix;
}

} // namespace

bool supportsAudio(const QString &path) {
    static const QSet<QString> extensions{QStringLiteral("wav"), QStringLiteral("mp3"), QStringLiteral("m4a"),
        QStringLiteral("flac"), QStringLiteral("aac"), QStringLiteral("ogg"), QStringLiteral("opus"),
        QStringLiteral("aiff"), QStringLiteral("aif"), QStringLiteral("aifc"), QStringLiteral("wma"),
        QStringLiteral("caf"), QStringLiteral("mka"), QStringLiteral("m4b")};
    return extensions.contains(extension(path));
}

bool supportsVideo(const QString &path) {
    static const QSet<QString> extensions{QStringLiteral("mp4"), QStringLiteral("mkv"), QStringLiteral("mov"),
        QStringLiteral("webm"), QStringLiteral("avi"), QStringLiteral("m4v"), QStringLiteral("mpeg"),
        QStringLiteral("mpg"), QStringLiteral("wmv"), QStringLiteral("ts"), QStringLiteral("mts"), QStringLiteral("m2ts")};
    return extensions.contains(extension(path));
}

QString mediaBackendError() { return ffmpeg().error; }

ExtractionResult extractMedia(const QString &path, const ExtractionOptions &options, bool video) {
    ExtractionResult result;
    result.next = options.start;
    result.state = QStringLiteral("complete");
    try {
        if (options.start < 0 || options.maxSegments < 1) throw MediaFailure(QStringLiteral("Invalid media extraction cursor or segment budget"));
        if (options.tempDir.isEmpty() || !QDir::isAbsolutePath(options.tempDir) || !QDir().mkpath(options.tempDir))
            throw MediaFailure(QStringLiteral("Media output must be a writable absolute directory"));
        if (!QDir::isAbsolutePath(path) || !QFileInfo(path).isFile()) throw MediaFailure(QStringLiteral("Media source is not an absolute local file"));
        FFmpeg &api = ffmpeg();
        if (!api.error.isEmpty()) throw MediaFailure(api.error);
        Collector collector{result, options, path, video ? QStringLiteral("video") : QStringLiteral("audio"),
            0, video ? std::min(maximumFrameBatch, options.maxSegments) : options.maxSegments};
        if (video && !extractVideo(api, path, collector)) result.complete = false;
        if (result.complete && !(options.cancelled && options.cancelled()) && !extractAudio(api, path, collector)) result.complete = false;
        if (options.cancelled && options.cancelled()) {
            result.complete = false;
            result.state = QStringLiteral("cancelled");
        } else if (!result.complete) result.state = QStringLiteral("partial");
    } catch (const std::exception &failure) {
        result.complete = false;
        result.state = options.cancelled && options.cancelled() ? QStringLiteral("cancelled") : QStringLiteral("failed");
        result.error = QString::fromUtf8(failure.what());
    }
    return result;
}

} // namespace qcopilots_search
