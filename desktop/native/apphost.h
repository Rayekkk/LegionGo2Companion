// SPDX-License-Identifier: BSD-3-Clause
#pragma once
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
public:
    AppHost(QString sessionPath, bool capture, QObject *parent=nullptr, QString bundlePath={});
    ~AppHost() override;
    static QString classifyService(const QByteArray &output);
    static bool registered(const QString &bundle, const QString &sessionPath);
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
signals:
    void changed();
    void quitRequested();
private:
    QString sessionPath_;
    QString bundlePath_;
    bool capture_=false, installed_=true, desktop_=false, trayAvailable_=false, starting_=false;
    QSettings settings_;
    QSystemTrayIcon tray_;
    QMenu menu_;
    QTimer monitor_, deadline_;
    QProcess service_;
    QQuickWindow *window_=nullptr;
    QString serviceState_="unknown", serviceError_;
    void update();
};
