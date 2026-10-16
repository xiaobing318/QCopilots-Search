#include "ipc_protocol.h"

#include "core/types.h"

#include <QJsonDocument>
#include <QJsonParseError>
#include <QtEndian>

namespace qcopilots_search {
namespace {
bool fail(QString *error, const QString &message)
{
    if (error)
        *error = message;
    return false;
}
}

QByteArray encodeIpcFrame(const QJsonObject &message, QString *error)
{
    if (error)
        error->clear();
    if (message.value(QStringLiteral("protocol")).toDouble(-1) != protocolVersion) {
        fail(error, QStringLiteral("Unsupported IPC protocol version"));
        return {};
    }
    const auto payload = QJsonDocument(message).toJson(QJsonDocument::Compact);
    if (payload.isEmpty() || payload.size() > maxMessageBytes) {
        fail(error, QStringLiteral("IPC message exceeds the maximum frame size"));
        return {};
    }
    QByteArray frame(4, Qt::Uninitialized);
    qToBigEndian<quint32>(static_cast<quint32>(payload.size()), frame.data());
    frame.append(payload);
    return frame;
}

bool takeIpcFrames(QByteArray &buffer, QList<QJsonObject> &messages, QString *error)
{
    if (error)
        error->clear();
    qsizetype offset = 0;
    while (buffer.size() - offset >= 4) {
        const auto length = qFromBigEndian<quint32>(buffer.constData() + offset);
        if (length == 0 || length > static_cast<quint32>(maxMessageBytes)) {
            buffer.clear();
            return fail(error, QStringLiteral("Invalid IPC frame size"));
        }
        if (buffer.size() - offset - 4 < static_cast<qsizetype>(length))
            break;
        const auto payload = QByteArrayView(buffer.constData() + offset + 4,
                                           static_cast<qsizetype>(length));
        QJsonParseError parseError;
        const auto document = QJsonDocument::fromJson(payload.toByteArray(), &parseError);
        if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
            buffer.clear();
            return fail(error, QStringLiteral("Invalid IPC JSON object: %1")
                                   .arg(parseError.errorString()));
        }
        const auto message = document.object();
        if (message.value(QStringLiteral("protocol")).toDouble(-1) != protocolVersion) {
            buffer.clear();
            return fail(error, QStringLiteral("Unsupported IPC protocol version"));
        }
        messages.append(message);
        offset += 4 + static_cast<qsizetype>(length);
    }
    if (offset)
        buffer.remove(0, offset);
    return true;
}

} // namespace qcopilots_search
