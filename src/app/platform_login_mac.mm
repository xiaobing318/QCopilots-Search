#include "login_startup.h"

#include <QGuiApplication>
#include <QStandardPaths>

#import <AppKit/AppKit.h>
#import <CoreServices/CoreServices.h>
#import <ServiceManagement/ServiceManagement.h>
#import <dispatch/dispatch.h>

namespace {
bool loginLaunch = false;
bool startupPrepared = false;
bool launchNotificationDelivered = false;
bool startupCompleted = false;
bool foregroundVariableWasSet = false;
QByteArray foregroundVariableValue;
constexpr const char *foregroundVariable = "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM";

bool currentEventIsLoginLaunch() {
    NSAppleEventDescriptor *event = [[NSAppleEventManager sharedAppleEventManager] currentAppleEvent];
    return event != nil && [event eventClass] == kCoreEventClass && [event eventID] == kAEOpenApplication
        && [[event paramDescriptorForKeyword:keyAEPropData] enumCodeValue] == keyAELaunchedAsLogInItem;
}

QString nativeError(NSError *error) {
    return error ? QString::fromUtf8([[error localizedDescription] UTF8String])
        : QStringLiteral("macOS did not provide an error description.");
}

bool hasApplicationBundle() {
    NSBundle *bundle = [NSBundle mainBundle];
    return [[[bundle bundleURL] pathExtension] isEqualToString:@"app"]
        && [[bundle bundleIdentifier] length] > 0;
}
}

namespace qcopilots_search {

bool macLoginStartupSupported() {
    @autoreleasepool {
        if (@available(macOS 13.0, *)) return hasApplicationBundle();
        return false;
    }
}

LoginStartupState macLoginStartupState() {
    @autoreleasepool {
        if (@available(macOS 13.0, *)) {
            if (!hasApplicationBundle())
                return {LoginStartupStatus::Unavailable, QStringLiteral("Login startup requires an installed macOS application bundle.")};
            switch ([[SMAppService mainAppService] status]) {
            case SMAppServiceStatusNotRegistered: return {LoginStartupStatus::Disabled, {}};
            case SMAppServiceStatusEnabled: return {LoginStartupStatus::Enabled, {}};
            case SMAppServiceStatusRequiresApproval:
                return {LoginStartupStatus::ApprovalRequired, QStringLiteral("Allow QCopilots Search in System Settings > General > Login Items.")};
            case SMAppServiceStatusNotFound:
                return {LoginStartupStatus::Error, QStringLiteral("macOS could not find this application's login service. Install the application before enabling login startup.")};
            }
            return {LoginStartupStatus::Error, QStringLiteral("macOS returned an unknown login service state.")};
        }
        return {LoginStartupStatus::Unavailable, QStringLiteral("Login startup requires macOS 13 or later.")};
    }
}

bool macLoginStartupSetEnabled(bool enabled, QString *error) {
    @autoreleasepool {
        const auto before = macLoginStartupState();
        const auto wanted = enabled ? LoginStartupStatus::Enabled : LoginStartupStatus::Disabled;
        if (before.status == wanted) return true;
        if (before.status == LoginStartupStatus::Unavailable
            || (enabled && before.status == LoginStartupStatus::ApprovalRequired)) {
            if (error) *error = before.message;
            return false;
        }
        if (@available(macOS 13.0, *)) {
            NSError *nativeFailure = nil;
            SMAppService *service = [SMAppService mainAppService];
            const BOOL accepted = enabled ? [service registerAndReturnError:&nativeFailure]
                                          : [service unregisterAndReturnError:&nativeFailure];
            const auto after = macLoginStartupState();
            if (accepted && after.status == wanted) return true;
            if (error) *error = after.status == LoginStartupStatus::ApprovalRequired ? after.message
                : QStringLiteral("Could not change the macOS login startup setting: %1").arg(nativeError(nativeFailure));
        } else if (error) {
            *error = before.message;
        }
        return false;
    }
}

void macPrepareLoginStartupDetection() {
    if (startupPrepared || QStandardPaths::isTestModeEnabled()) return;
    @autoreleasepool {
        // Only packaged apps receive SMAppService main-app login launches.
        // Do not change the foreground policy of bare developer executables.
        if (!hasApplicationBundle()) return;
        startupPrepared = true;
        foregroundVariableWasSet = qEnvironmentVariableIsSet(foregroundVariable);
        foregroundVariableValue = qgetenv(foregroundVariable);
        // Qt 6.12 activates in applicationDidFinishLaunching even without a
        // visible window. Hold this guard until every launch observer returns.
        qputenv(foregroundVariable, QByteArrayLiteral("1"));
        NSNotificationCenter *center = [NSNotificationCenter defaultCenter];
        [center addObserverForName:NSApplicationWillFinishLaunchingNotification object:nil queue:nil usingBlock:^(NSNotification *) {
            loginLaunch = loginLaunch || currentEventIsLoginLaunch();
        }];
        [center addObserverForName:NSApplicationDidFinishLaunchingNotification object:nil queue:nil usingBlock:^(NSNotification *) {
            loginLaunch = loginLaunch || currentEventIsLoginLaunch();
            // Observers and the Qt application delegate have no specified
            // ordering. Never restore inside this synchronous notification.
            dispatch_async(dispatch_get_main_queue(), ^{
                launchNotificationDelivered = true;
            });
        }];
    }
}

bool macCompleteLoginStartupDetection(bool showForeground) {
    if (!startupPrepared || startupCompleted) return true;
    const bool nativeCocoa = qobject_cast<QGuiApplication *>(QCoreApplication::instance())
        && QGuiApplication::platformName() == QStringLiteral("cocoa");
    // An offscreen/minimal QPA has no NSApplication launch notification. Restore
    // the process environment immediately and do not activate AppKit there.
    if (nativeCocoa && !launchNotificationDelivered) return false;
    @autoreleasepool {
        startupCompleted = true;
        if (foregroundVariableWasSet) qputenv(foregroundVariable, foregroundVariableValue);
        else qunsetenv(foregroundVariable);
        // Match Qt's existing non-empty-variable opt-out, including values such
        // as "0". Restore normal foreground launch only when it was requested.
        if (nativeCocoa && showForeground && foregroundVariableValue.isEmpty() && !QStandardPaths::isTestModeEnabled())
            [NSApp activateIgnoringOtherApps:YES];
        return true;
    }
}

bool macIsLoginStartupLaunch() {
    @autoreleasepool {
        loginLaunch = loginLaunch || currentEventIsLoginLaunch();
        return loginLaunch;
    }
}

} // namespace qcopilots_search
