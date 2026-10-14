// SPDX-License-Identifier: BSD-3-Clause
#include "apphost.h"
#include "diagnostics.h"
#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QGuiApplication>
#include <QPixmap>

AppHost::AppHost(QString path, bool capture, QObject *parent, QString bundlePath)
    : QObject(parent), sessionPath_(std::move(path)), bundlePath_(bundlePath.isEmpty()?QCoreApplication::applicationDirPath():std::move(bundlePath)), capture_(capture) {
    // The installer publishes this same asset before the backend session.
    // Decode through the image plugin; the runtime needs no SVG icon engine.
    const QIcon icon{QPixmap(QDir(QFileInfo(sessionPath_).absolutePath()).filePath("icon.svg"))};
    QGuiApplication::setWindowIcon(icon);
    tray_.setIcon(icon);tray_.setToolTip("Legion Go Companion");
    menu_.addAction("Show Companion",this,&AppHost::showWindow);
    menu_.addAction("Hide window",this,[this]{hideWindow();});
    menu_.addSeparator();menu_.addAction("Quit",this,&AppHost::quitRequested);
    tray_.setContextMenu(&menu_);
    connect(&tray_,&QSystemTrayIcon::activated,this,[this](QSystemTrayIcon::ActivationReason reason){
        if(reason==QSystemTrayIcon::Trigger || reason==QSystemTrayIcon::DoubleClick)showWindow();
    });
    monitor_.setInterval(2000);connect(&monitor_,&QTimer::timeout,this,&AppHost::update);
    deadline_.setSingleShot(true);connect(&deadline_,&QTimer::timeout,this,[this]{
        CompanionDiagnostics::event(starting_?"service_start_deadline":"service_status_deadline");
        serviceError_=starting_?"The service start was not confirmed. Check its status before trying again.":"Could not read the Decky service status.";
        service_.kill();emit changed(); // Only this app's bounded systemctl/pkexec child.
    });
    connect(&service_,&QProcess::errorOccurred,this,[this](QProcess::ProcessError problem){
        CompanionDiagnostics::event("service_process_failed",{},0,0,int(problem));
        deadline_.stop();serviceError_="Could not run the Decky service action.";
        if(problem==QProcess::FailedToStart) { starting_=false;serviceState_="unknown"; }
        emit changed();
    });
    connect(&service_,qOverload<int,QProcess::ExitStatus>(&QProcess::finished),this,[this](int code,QProcess::ExitStatus status){
        deadline_.stop();
        if(starting_) {
            CompanionDiagnostics::event(code || status!=QProcess::NormalExit?"service_start_failed":"service_start_completed",{},0,0,code);
            starting_=false;
            if(code || status!=QProcess::NormalExit)serviceError_="Decky was not started. Authorization may have been cancelled.";
            QTimer::singleShot(0,this,&AppHost::inspectService);
        } else if(code==0 && status==QProcess::NormalExit) {
            serviceState_=classifyService(service_.readAllStandardOutput());
        } else serviceState_="unknown";
        CompanionDiagnostics::change("service_state",serviceState_);
        emit changed();
    });
    if(!capture_) { update();monitor_.start(); }
}
AppHost::~AppHost() {
    service_.disconnect(this);
    if(serviceBusy()) { service_.kill();service_.waitForFinished(1000); }
}
QString AppHost::classifyService(const QByteArray &output) {
    QString active,pid;int states=0,pids=0;
    for(const auto &line:QString::fromUtf8(output).split('\n',Qt::SkipEmptyParts)) {
        if(line.startsWith("ActiveState=")) { active=line.mid(12);++states; }
        if(line.startsWith("MainPID=")) { pid=line.mid(8);++pids; }
    }
    if(states!=1 || pids!=1)return "unknown";
    if((active=="inactive" || active=="failed") && pid=="0")return active;
    return active=="active"?"active":"unknown";
}
bool AppHost::registered(const QString &bundle, const QString &sessionPath) {
    const QFileInfo metadata(QDir(bundle).filePath("../../plugin.json"));
    const QFileInfo runtime(QDir(QFileInfo(sessionPath).absolutePath()).filePath("runtime.json"));
    return metadata.isFile() && !metadata.isSymLink() && runtime.isFile() && !runtime.isSymLink();
}
void AppHost::setWindow(QQuickWindow *window) { window_=window; }
void AppHost::setTrayEnabled(bool value) {
    if(capture_ || !desktop_)return;
    settings_.setValue("trayEnabled",value);settings_.sync();update();
}
void AppHost::setCloseToTray(bool value) { if(!capture_ && desktop_) { settings_.setValue("closeToTray",value);settings_.sync();emit changed(); } }
void AppHost::setContext(bool fresh, bool gamescope) {
    const bool next=fresh?!gamescope:desktop_; // No guessed mode from a missing RPC sample.
    if(next!=desktop_) { desktop_=next;update(); }
}
void AppHost::update() {
    if(capture_) { emit changed();return; }
    const bool before=installed_, available=trayAvailable_, visible=tray_.isVisible();
    installed_=registered(bundlePath_,sessionPath_);
    trayAvailable_=!capture_ && QSystemTrayIcon::isSystemTrayAvailable();
    tray_.setVisible(installed_ && desktop_ && trayAvailable_ && trayEnabled());
    CompanionDiagnostics::change("installation",installed_?"present":"missing");
    CompanionDiagnostics::change("tray",tray_.isVisible()?"visible":"hidden");
    if(visible && !tray_.isVisible() && window_ && !window_->isVisible())showWindow();
    if(before && !installed_)showWindow();
    if(before!=installed_ || available!=trayAvailable_ || visible!=tray_.isVisible())emit changed();
}
bool AppHost::hideWindow() {
    if(!window_ || !tray_.isVisible())return false;
    for(auto *child:window_->findChildren<QQuickWindow *>())child->hide();
    window_->hide();return true;
}
void AppHost::showWindow() { if(window_) { window_->showNormal();window_->raise();window_->requestActivate(); } }
bool AppHost::canStartService() const {
#ifdef Q_OS_LINUX
    return !capture_ && installed_ && desktop_ && !serviceBusy() && (serviceState_=="inactive" || serviceState_=="failed");
#else
    return false;
#endif
}
void AppHost::inspectService() {
#ifdef Q_OS_LINUX
    if(capture_ || serviceBusy())return;
    service_.setProgram("/usr/bin/systemctl");service_.setArguments({"show","plugin_loader.service","--property=ActiveState","--property=MainPID"});
    service_.start();deadline_.start(2000);emit changed();
#endif
}
void AppHost::startService() {
    if(!canStartService()) { CompanionDiagnostics::event("service_start_unavailable");return; }
    CompanionDiagnostics::event("service_start_requested",{},0,0,0,-1,false);
    // Never restart/kill an active or hung loader. Start is harmless if another
    // client has brought the service up since the status check.
    serviceError_.clear();starting_=true;
    service_.setProgram("/usr/bin/pkexec");service_.setArguments({"/usr/bin/systemctl","start","plugin_loader.service"});
    service_.start();deadline_.start(30000);emit changed();
}
