#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFontDatabase>
#include <QFontMetrics>
#include <QGuiApplication>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPainter>
#include <QRawFont>
#include <QSysInfo>
#include <cstdio>

int main(int argc, char **argv)
{
    QGuiApplication app(argc, argv);
    const auto arguments = app.arguments();
    if (arguments.size() != 4) {
        std::fprintf(stderr, "Usage: qcopilots-search-generate-ocr-fixtures FONT_CACHE OUTPUT_DIR MANIFEST_PATH\n");
        return 2;
    }
    const QDir fontRoot(arguments[1]);
    const QDir output(arguments[2]);
    if (!QDir().mkpath(output.absolutePath())) return 3;
    struct Script { const char *id; const char *font; const char *sha; const char *phrase; };
    const Script scripts[]{
        {"simplified", "NotoSansCJKsc-Regular.otf", "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b", "安全出口 禁止吸烟"},
        {"traditional", "NotoSansCJKtc-Regular.otf", "dce08bd4fd91aa8aa76ed8fea4b694c2dfb8550f67871e326843212ddbeb88b4", "社區會議 歡迎參加"}
    };
    QJsonArray fixtures;
    for (const auto &script : scripts) {
        QFile fontFile(fontRoot.filePath(QString::fromLatin1(script.font)));
        if (!fontFile.open(QIODevice::ReadOnly)) return 4;
        const auto fontBytes = fontFile.readAll();
        const auto fontSha = QCryptographicHash::hash(fontBytes, QCryptographicHash::Sha256).toHex();
        if (fontSha != QByteArray(script.sha)) return 5;
        const int id = QFontDatabase::addApplicationFontFromData(fontBytes);
        const auto families = QFontDatabase::applicationFontFamilies(id);
        if (families.isEmpty()) return 6;
        for (const int pixelSize : {40, 80}) {
            QFont font(families.first()); font.setPixelSize(pixelSize); font.setWeight(QFont::Normal);
            const auto phrase = QString::fromUtf8(script.phrase);
            const auto raw = QRawFont::fromFont(font);
            for (const char32_t character : phrase.toUcs4())
                if (!raw.supportsCharacter(character)) return 7;
            const QFontMetrics metrics(font);
            constexpr int margin = 60;
            QImage image(metrics.horizontalAdvance(phrase) + 2 * margin, metrics.height() + 2 * margin, QImage::Format_RGB32);
            image.fill(Qt::white);
            { QPainter painter(&image); painter.setFont(font); painter.setPen(Qt::black);
              painter.drawText(margin, margin + metrics.ascent(), phrase); }
            const QString name = QStringLiteral("qcopilots-search-ocr-%1-%2px.png").arg(QString::fromLatin1(script.id)).arg(pixelSize);
            if (!image.save(output.filePath(name))) return 8;
            QFile png(output.filePath(name)); if (!png.open(QIODevice::ReadOnly)) return 9;
            fixtures.append(QJsonObject{{"file", name}, {"sha256", QString::fromLatin1(QCryptographicHash::hash(png.readAll(), QCryptographicHash::Sha256).toHex())},
                {"phrase", phrase}, {"pixelSize", pixelSize}, {"width", image.width()}, {"height", image.height()},
                {"font", QString::fromLatin1(script.font)}, {"fontSha256", QString::fromLatin1(fontSha)}});
        }
        QFontDatabase::removeApplicationFont(id);
    }
    QFile manifest(arguments[3]); if (!manifest.open(QIODevice::WriteOnly)) return 10;
    const QJsonObject report{{"schemaVersion", 1}, {"qt", QString::fromLatin1(qVersion())},
        {"generatorPlatform", QSysInfo::prettyProductName()}, {"fontRevision", "523d033d6cb47f4a80c58a35753646f5c3608a78"},
        {"fontRelease", "Noto Sans CJK Sans2.004"}, {"margin", 60}, {"fixtures", fixtures}};
    return manifest.write(QJsonDocument(report).toJson()) > 0 ? 0 : 11;
}
