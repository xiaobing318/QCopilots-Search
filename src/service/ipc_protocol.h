#pragma once

#include <QByteArray>
#include <QJsonObject>
#include <QList>
#include <QString>

namespace qcopilots_search {

// A frame is a four-byte big-endian payload length followed by a UTF-8 JSON
// object. Both directions require the same protocol version.
QByteArray encodeIpcFrame(const QJsonObject &message, QString *error = nullptr);

// Appends complete messages to messages and retains only an incomplete final
// frame in buffer. On any malformed frame returns false and clears buffer.
// Callers must discard messages and close the connection on failure.
bool takeIpcFrames(QByteArray &buffer, QList<QJsonObject> &messages,
                   QString *error = nullptr);

} // namespace qcopilots_search
