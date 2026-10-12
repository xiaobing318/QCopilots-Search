#include <QCoreApplication>
#include <QCryptographicHash>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <memory>
#include <csetjmp>
#include <cstdlib>
#include <cstdio>
#include <iterator>
#include <jpeglib.h>
#include <jerror.h>
#include <libraw/libraw.h>
static_assert(JPEG_LIB_VERSION == 62, "The JPEG diagnostic must use the pinned API 62 headers");
#ifdef Q_OS_WIN
#include <windows.h>
#else
#include <dlfcn.h>
#endif

namespace {
QString jpegLibraryPath()
{
#ifdef Q_OS_WIN
    // The public JPEG header does not use dllimport, so taking the linked
    // function's address can identify an executable import thunk instead.
    // Resolve the already loaded, pinned module's actual export without
    // loading another library or changing the loader search order.
    const HMODULE loaded = GetModuleHandleW(L"jpeg62.dll");
    const auto entry = loaded ? GetProcAddress(loaded, "jpeg_CreateDecompress") : nullptr;
    HMODULE module = nullptr;
    if (!entry || !GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(entry), &module)) return {};
    wchar_t path[4096]{};
    const auto length = GetModuleFileNameW(module, path, DWORD(std::size(path)));
    return length && length < std::size(path) ? QString::fromWCharArray(path, int(length)) : QString{};
#else
    Dl_info info{};
    const auto *entry = dlsym(RTLD_DEFAULT, "jpeg_CreateDecompress");
    return entry && dladdr(entry, &info) && info.dli_fname
        ? QString::fromLocal8Bit(info.dli_fname) : QString{};
#endif
}

struct JpegProbe {
    jpeg_error_mgr error{};
    jpeg_decompress_struct decoder{};
    std::jmp_buf recovery;
    char message[JMSG_LENGTH_MAX]{};
    const char *stage = "create";
    unsigned char *row = nullptr;
    int code = 0;
    bool created = false;
    bool complete = false;
};

QJsonObject probeJpeg(const QByteArray &bytes)
{
    // Mutable state lives on the heap. The longjmp crosses only libjpeg C
    // frames, with no intervening C++ object construction or destruction.
    auto probe = std::make_unique<JpegProbe>();
    probe->decoder.err = jpeg_std_error(&probe->error);
    probe->decoder.client_data = probe.get();
    probe->error.error_exit = [](j_common_ptr context) {
        auto *state = static_cast<JpegProbe *>(context->client_data);
        state->code = context->err->msg_code;
        context->err->format_message(context, state->message);
        std::longjmp(state->recovery, 1);
    };
    if (setjmp(probe->recovery) == 0) {
        jpeg_create_decompress(&probe->decoder);
        probe->created = true;
        probe->stage = "memory-source";
        jpeg_mem_src(&probe->decoder, reinterpret_cast<const unsigned char *>(bytes.constData()),
            static_cast<unsigned long>(bytes.size()));
        probe->stage = "header";
        jpeg_read_header(&probe->decoder, TRUE);
        probe->stage = "start";
        jpeg_start_decompress(&probe->decoder);
        // This diagnostic consumes the locked 64 x 64 RGB fixture only.
        if (probe->decoder.output_width == 64 && probe->decoder.output_height == 64
                && probe->decoder.output_components == 3) {
            probe->row = static_cast<unsigned char *>(std::malloc(64 * 3));
            if (probe->row) {
                probe->stage = "scanlines";
                for (int row = 0; row < 64; ++row)
                    if (jpeg_read_scanlines(&probe->decoder, &probe->row, 1) != 1) break;
                if (probe->decoder.output_scanline == probe->decoder.output_height) {
                    probe->stage = "finish";
                    probe->complete = jpeg_finish_decompress(&probe->decoder);
                }
            }
        }
    }
    QJsonObject report{{"headerApi", JPEG_LIB_VERSION},
        {"runtimeApi", probe->created ? QJsonValue(JPEG_LIB_VERSION) : QJsonValue(QJsonValue::Null)},
        {"runtimeVersion", probe->created && probe->error.jpeg_message_table
                && JMSG_VERSION <= probe->error.last_jpeg_message
            ? QJsonValue(QString::fromLatin1(probe->error.jpeg_message_table[JMSG_VERSION]))
            : QJsonValue(QJsonValue::Null)},
        {"decompressStructBytes", qint64(sizeof(jpeg_decompress_struct))},
        {"errorStructBytes", qint64(sizeof(jpeg_error_mgr))},
        {"library", jpegLibraryPath()}, {"stage", QString::fromLatin1(probe->stage)},
        {"errorCode", probe->code}, {"error", QString::fromLatin1(probe->message)},
        {"width", qint64(probe->decoder.output_width)}, {"height", qint64(probe->decoder.output_height)},
        {"components", probe->decoder.output_components}, {"complete", probe->complete}};
    // Successful creation verifies both runtime API and struct size. libjpeg
    // reports the actual mismatching value through format_message on failure.
    jpeg_destroy_decompress(&probe->decoder);
    std::free(probe->row);
    return report;
}

struct RawJpegTrace {
    void (*originalError)(j_common_ptr) = nullptr;
    char message[JMSG_LENGTH_MAX]{};
    int code = 0;
    int sources = 0;
    qint64 offset = -1;
    qint64 bytes = 0;
    int sourceStatus = -1;
};

class TracedRawStream final : public LibRaw_buffer_datastream {
public:
    TracedRawStream(const QByteArray &bytes, RawJpegTrace &trace)
        : LibRaw_buffer_datastream(bytes.constData(), size_t(bytes.size())), trace_(trace) {}
    int jpeg_src(void *value) override
    {
        auto *context = static_cast<jpeg_decompress_struct *>(value);
        ++trace_.sources;
        trace_.offset = tell(); trace_.bytes = size();
        // Several tiles may reuse the same cinfo. Never remember our own
        // wrapper as the original error callback, which would recurse.
        if (context->err->error_exit != reportError) trace_.originalError = context->err->error_exit;
        context->client_data = &trace_;
        context->err->error_exit = reportError;
        trace_.sourceStatus = LibRaw_buffer_datastream::jpeg_src(value);
        return trace_.sourceStatus;
    }
private:
    static void reportError(j_common_ptr context)
    {
        auto *trace = static_cast<RawJpegTrace *>(context->client_data);
        trace->code = context->err->msg_code;
        context->err->format_message(context, trace->message);
        trace->originalError(context); // Preserve LibRaw's existing exception handling.
        std::abort(); // A libjpeg error_exit callback must not return.
    }
    RawJpegTrace &trace_;
};

QJsonObject probeRaw(const QByteArray &bytes, bool traceJpeg)
{
    RawJpegTrace trace;
    TracedRawStream stream(bytes, trace);
    // open_datastream leaves ownership with the caller. The decoder must be
    // destroyed before stream and bytes, just like the owned open_buffer path.
    auto decoder = std::make_unique<LibRaw>();
    decoder->imgdata.params.half_size = 1;
    decoder->imgdata.params.output_bps = 8;
    decoder->imgdata.params.output_color = 1;
    decoder->imgdata.params.use_camera_wb = 1;
    const int opened = traceJpeg ? decoder->open_datastream(&stream)
        : decoder->open_buffer(const_cast<char *>(bytes.constData()), size_t(bytes.size()));
    QJsonObject report{{"open", opened}, {"libraw", QString::fromLatin1(LibRaw::version())}};
    if (opened == LIBRAW_SUCCESS) {
        libraw_decoder_info_t info{};
        decoder->get_decoder_info(&info);
        report.insert("decoder", QString::fromLatin1(info.decoder_name ? info.decoder_name : ""));
        report.insert("colors", decoder->imgdata.idata.colors);
        report.insert("rawWidth", decoder->imgdata.sizes.raw_width);
        report.insert("rawHeight", decoder->imgdata.sizes.raw_height);
        const int unpacked = decoder->unpack();
        report.insert("unpack", unpacked);
        if (unpacked == LIBRAW_SUCCESS) {
            const int processed = decoder->dcraw_process();
            report.insert("process", processed);
            if (processed == LIBRAW_SUCCESS) {
                int status = 0;
                auto *image = decoder->dcraw_make_mem_image(&status);
                report.insert("memoryImage", status);
                report.insert("complete", image && status == LIBRAW_SUCCESS);
                if (image) {
                    report.insert("width", image->width); report.insert("height", image->height);
                    LibRaw::dcraw_clear_mem(image);
                }
            }
        }
    }
    report.insert("jpegSources", trace.sources);
    report.insert("jpegSourceOffset", trace.offset);
    report.insert("jpegSourceBytes", trace.bytes);
    report.insert("jpegSourceStatus", trace.sourceStatus);
    report.insert("jpegErrorCode", trace.code);
    report.insert("jpegError", QString::fromLatin1(trace.message));
    return report;
}
} // namespace

int main(int argc, char **argv)
{
    QCoreApplication application(argc, argv);
    QCoreApplication::setApplicationName(QStringLiteral("qcs-tests-jpeg-probe"));
    if (application.arguments().size() != 2) return 2;
    QFile fixture(application.arguments().at(1));
    if (!QFileInfo(fixture.fileName()).isAbsolute() || !fixture.open(QIODevice::ReadOnly)) return 2;
    const QByteArray dng = fixture.readAll();
    const auto sha = QCryptographicHash::hash(dng, QCryptographicHash::Sha256).toHex();
    if (sha != QByteArray("65eb7a5050b3f87172b9a424cfa9b7652adc0c30a260b1d4b971e14c2a1fb28b")
            || dng.size() != 1576 || dng.mid(414, 2) != QByteArray::fromHex("ffd8")
            || dng.right(2) != QByteArray::fromHex("ffd9")) return 2;
    // Offset and byte count are the locked fixture's TIFF strip metadata.
    const auto jpeg = probeJpeg(dng.mid(414, 1162));
    const auto buffer = probeRaw(dng, false);
    const auto traced = probeRaw(dng, true);
    const QJsonObject report{{"fixtureSha256", QString::fromLatin1(sha)}, {"jpeg", jpeg},
        {"rawBuffer", buffer}, {"rawTracedBuffer", traced}};
    const auto json = QJsonDocument(report).toJson(QJsonDocument::Indented);
    QFile output;
    if (!output.open(stdout, QIODevice::WriteOnly) || output.write(json) != json.size() || !output.flush()) return 2;
    return jpeg.value("complete").toBool() && buffer.value("complete").toBool()
        && traced.value("complete").toBool() ? 0 : 1;
}
