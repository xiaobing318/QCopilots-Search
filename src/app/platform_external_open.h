#pragma once

#include "core/app_settings.h"
#include "core/types.h"
#include <QStringList>
#include <optional>

namespace qcopilots_search {

// Construction is pure so argument and location contracts can be tested without
// launching applications or changing the user's file associations.
struct ExternalOpenCommand {
    bool systemOpen = false;
    QString program;
    QStringList arguments;
    bool waitForLauncher = false;
    QString notice;
};

QString expandOpenArgument(const QString &argument, const SearchHit &hit);
QString resolveExternalExecutable(const QString &program, QString *error = nullptr);
std::optional<ExternalOpenCommand> buildOpenCommand(const SearchHit &hit,
                                                   const AppSettings &settings,
                                                   QString *error = nullptr);
bool executeOpenCommand(const SearchHit &hit, const ExternalOpenCommand &command,
                        QString *error = nullptr);

} // namespace qcopilots_search
