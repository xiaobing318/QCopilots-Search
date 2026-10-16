#include "legacy_doc_extractor.h"
#include "core/utf8_locale.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QVector>
#include <algorithm>
#include <stdexcept>

#include <functor.h>
#include <handlers.h>
#include <parser.h>
#include <parserfactory.h>
#include <ustring.h>

namespace qcopilots_search::legacy_doc {
namespace {

class ExtractionError final : public std::runtime_error {
public:
    explicit ExtractionError(const char *message) : std::runtime_error(message) {}
};

class TextCollector final : public wvWare::TextHandler,
                            public wvWare::TableHandler,
                            public wvWare::SubDocumentHandler {
public:
    TextCollector(qsizetype limit, const std::function<bool()> &cancelled)
        : limit_(limit), cancelled_(cancelled) {}

    QString result() const { return text_.trimmed(); }

    void runOfText(const wvWare::UString &value,
                   wvWare::SharedPtr<const wvWare::Word97::CHP>) override {
        check();
        // Field instructions can contain URLs, paths or control code. Index the
        // displayed result, including nested fields, without executing fields.
        if (std::find(fieldResults_.cbegin(), fieldResults_.cend(), false) != fieldResults_.cend())
            return;
        for (int i = 0; i < value.length(); ++i) {
            const QChar character(value[i].unicode());
            if (character == u'\r') append(u'\n');
            else if (character == u'\t' || character == u'\n' || character.unicode() >= 0x20)
                append(character);
        }
    }

    void paragraphEnd() override { boundary(); }
    void pageBreak() override { boundary(); }
    void sectionEnd() override { boundary(); }
    void tableCellEnd() override { append(u'\t'); }
    void tableRowEnd() override { boundary(); }
    void headerStart(wvWare::HeaderData::Type) override { boundary(); }
    void headerEnd() override { boundary(); }
    void footnoteStart() override { boundary(); }
    void footnoteEnd() override { boundary(); }
    void annotationStart() override { boundary(); }
    void annotationEnd() override { boundary(); }
    void setProgress(int) override { check(); }

    void headersFound(const wvWare::HeaderFunctor &parse) override { visit(parse); }
    void tableRowFound(const wvWare::TableRowFunctor &parse,
                       wvWare::SharedPtr<const wvWare::Word97::TAP>) override { visit(parse); }
    void footnoteFound(wvWare::FootnoteData, wvWare::UString,
                       wvWare::SharedPtr<const wvWare::Word97::SEP>,
                       wvWare::SharedPtr<const wvWare::Word97::CHP>,
                       const wvWare::FootnoteFunctor &parse) override { visit(parse); }
    void annotationFound(wvWare::UString,
                         wvWare::SharedPtr<const wvWare::Word97::CHP>,
                         const wvWare::AnnotationFunctor &parse) override { visit(parse); }

    void fieldStart(const wvWare::FLD *, wvWare::SharedPtr<const wvWare::Word97::CHP>) override {
        check();
        if (fieldResults_.size() >= 64) throw ExtractionError("DOC field nesting exceeds limit");
        fieldResults_.append(false);
    }
    void fieldSeparator(const wvWare::FLD *, wvWare::SharedPtr<const wvWare::Word97::CHP>) override {
        if (!fieldResults_.isEmpty()) fieldResults_.last() = true;
    }
    void fieldEnd(const wvWare::FLD *, wvWare::SharedPtr<const wvWare::Word97::CHP>) override {
        if (!fieldResults_.isEmpty()) fieldResults_.removeLast();
    }

private:
    void check() const {
        if (cancelled_ && cancelled_()) throw ExtractionError("DOC extraction cancelled");
    }
    void append(QChar character) {
        if (text_.size() >= limit_) throw ExtractionError("DOC extracted text exceeds size limit");
        text_.append(character);
    }
    void boundary() {
        check();
        if (!text_.isEmpty() && text_.back() != u'\n') append(u'\n');
    }
    template<typename Functor> void visit(const Functor &parse) {
        check();
        if (depth_ >= 64) throw ExtractionError("DOC subdocument nesting exceeds limit");
        ++depth_;
        try {
            parse();
        } catch (...) {
            --depth_;
            throw;
        }
        --depth_;
    }
    QString text_;
    QVector<bool> fieldResults_;
    qsizetype limit_;
    const std::function<bool()> &cancelled_;
    int depth_ = 0;
};

} // namespace

QString extract(const QString &path, QString *error, qsizetype maxCharacters,
                const std::function<bool()> &cancelled) {
    if (error) error->clear();
    try {
        if (maxCharacters <= 0) throw ExtractionError("DOC text limit must be positive");
        if (cancelled && cancelled()) throw ExtractionError("DOC extraction cancelled");
        // Check the container before entering the legacy parser. Never accept an
        // arbitrary binary stream as text when a document is malformed.
        QFile file(path);
        if (!file.open(QIODevice::ReadOnly)) {
            if (error) *error = QStringLiteral("Cannot read DOC: ") + file.errorString();
            return {};
        }
        if (file.size() < 512 || file.read(8) != QByteArray::fromHex("d0cf11e0a1b11ae1"))
            throw ExtractionError("Invalid binary DOC compound-file signature");
        file.close();

        TextCollector collector(maxCharacters, cancelled);
        // Upstream POLE opens narrow paths. Limit the Windows CRT adaptation to
        // this thread and keep it alive until the parser closes every stream.
        ScopedUtf8Locale utf8Locale;
        if (!utf8Locale.isValid())
            throw ExtractionError("Cannot initialize UTF-8 locale for DOC extraction");
        QString absolutePath = QDir::cleanPath(QFileInfo(path).absoluteFilePath());
#ifdef Q_OS_WIN
        absolutePath = QDir::toNativeSeparators(absolutePath);
        if (!absolutePath.startsWith(QStringLiteral("\\\\?\\"))) {
            if (absolutePath.startsWith(QStringLiteral("\\\\")))
                absolutePath = QStringLiteral("\\\\?\\UNC\\") + absolutePath.mid(2);
            else
                absolutePath.prepend(QStringLiteral("\\\\?\\"));
        }
#endif
        const QByteArray utf8Path = absolutePath.toUtf8();
        auto parser = wvWare::ParserFactory::createParser(
            std::string(utf8Path.constData(), size_t(utf8Path.size())));
        if (!parser || !parser->isOk())
            throw ExtractionError("Unsupported, encrypted or malformed binary DOC");
        if (parser->fib().fEncrypted)
            throw ExtractionError("Encrypted DOC is unsupported");
        parser->setTextHandler(&collector);
        parser->setTableHandler(&collector);
        parser->setSubDocumentHandler(&collector);
        if (!parser->parse()) throw ExtractionError("Binary DOC parser reported an incomplete document");
        if (cancelled && cancelled()) throw ExtractionError("DOC extraction cancelled");
        return collector.result();
    } catch (const std::exception &exception) {
        if (error) *error = QString::fromUtf8(exception.what());
    } catch (...) {
        if (error) *error = QStringLiteral("Binary DOC extraction failed with an unknown parser error");
    }
    return {};
}

} // namespace qcopilots_search::legacy_doc
