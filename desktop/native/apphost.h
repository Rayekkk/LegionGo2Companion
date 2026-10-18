// SPDX-License-Identifier: BSD-3-Clause
#pragma once
#include <QElapsedTimer>
#include <QHash>
#include <QObject>
#include <QProcess>
#include <QSettings>
#include <QSystemTrayIcon>
#include <QMenu>
#include <QTimer>
#include <QQuickWindow>

// Desktop presentation and one explicit service-start action. No hardware RPCs.
class AppHost final : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool installed READ installed NOTIFY changed)
    Q_PROPERTY(bool desktopMode READ desktopMode NOTIFY changed)
    Q_PROPERTY(bool trayAvailable READ trayAvailable NOTIFY changed)
    Q_PROPERTY(bool trayEnabled READ trayEnabled WRITE setTrayEnabled NOTIFY changed)
    Q_PROPERTY(bool trayVisible READ trayVisible NOTIFY changed)
    Q_PROPERTY(bool closeToTray READ closeToTray WRITE setCloseToTray NOTIFY changed)
    Q_PROPERTY(QString serviceState READ serviceState NOTIFY changed)
    Q_PROPERTY(bool serviceBusy READ serviceBusy NOTIFY changed)
    Q_PROPERTY(QString serviceError READ serviceError NOTIFY changed)
    Q_PROPERTY(bool canStartService READ canStartService NOTIFY changed)
    Q_PROPERTY(bool updateWaiting READ updateWaiting NOTIFY changed)
public:
    AppHost(QString sessionPath, bool capture, QObject *parent=nullptr);
    ~AppHost() override;
    static QString classifyService(const QByteArray &output);
    // runtime.json next to the session names the plugin folder, and its plugin.json still exists.
    static bool registered(const QString &sessionPath);
    static int trayCheckMs(bool hiddenInTray) { return hiddenInTray?1500:30000; }
    // What to do once this executable was replaced or removed on disk: "stay", "quit" (plugin
    // uninstalled), "wait" (the new version is not registered yet), "ask" (an unsaved edit is open)
    // or "restart" (start the new version, then quit).
    static QString replacedStep(bool replaced, bool launcher, bool registered, bool editing);
    // The launcher's own precondition: runtime.json names an executable with the recorded SHA-256.
    static bool registrationReady(const QString &runtimePath);
    // A game's name from Steam's files under steamRoot (app manifests, non-Steam shortcuts), or "".
    static QString steamName(const QString &steamRoot, const QString &appId);
    bool updateWaiting() const { return updateWaiting_; }
    bool relaunchRequested() const { return relaunch_; }
    QString launcherPath() const;
    void setWindow(QQuickWindow *window);
    bool installed() const { return installed_; }
    bool desktopMode() const { return desktop_; }
    bool trayAvailable() const { return trayAvailable_; }
    bool trayEnabled() const { return settings_.value("trayEnabled",true).toBool(); }
    bool closeToTray() const { return settings_.value("closeToTray",false).toBool(); }
    bool trayVisible() const { return tray_.isVisible(); }
    QString serviceState() const { return serviceState_; }
    bool serviceBusy() const { return service_.state()!=QProcess::NotRunning; }
    QString serviceError() const { return serviceError_; }
    bool canStartService() const;
    void setTrayEnabled(bool value);
    void setCloseToTray(bool value);
    Q_INVOKABLE void setContext(bool fresh, bool gamescope);
    Q_INVOKABLE bool hideWindow();
    Q_INVOKABLE void showWindow();
    Q_INVOKABLE void inspectService();
    Q_INVOKABLE void startService();
    Q_INVOKABLE void restartNow();
    Q_INVOKABLE QString gameName(const QString &appId);
    // Desktop Mode login (autostart): the window stays hidden until the tray icon is used.
    void startInTray();
signals:
    void changed();
    void quitRequested();
private:
    QString sessionPath_;
    bool capture_=false, installed_=true, desktop_=false, trayAvailable_=false, starting_=false, updateWaiting_=false, relaunch_=false;
    QSettings settings_;
    QSystemTrayIcon tray_;
    QMenu menu_;
    QTimer monitor_, deadline_;
    QProcess service_;
    QQuickWindow *window_=nullptr;
    QString serviceState_="unknown", serviceError_;
    QHash<QString,QString> names_;
    QElapsedTimer trayCheck_; // isSystemTrayAvailable() is a synchronous D-Bus call on Linux
    void update(bool recheckTray=false);
    void checkReplaced();
};
