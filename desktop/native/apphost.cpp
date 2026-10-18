// SPDX-License-Identifier: BSD-3-Clause
#include "apphost.h"
#include "diagnostics.h"
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPixmap>
#include <QRegularExpression>
#include <QtEndian>
#ifdef Q_OS_LINUX
#include <unistd.h>
#endif

AppHost::AppHost(QString path, bool capture, QObject *parent)
    : QObject(parent), sessionPath_(std::move(path)), capture_(capture) {
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
    monitor_.setInterval(2000);connect(&monitor_,&QTimer::timeout,this,[this]{update();});
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
static QJsonObject runtimeConfig(const QString &runtimePath) {
    QFile runtime(runtimePath);
    if(QFileInfo(runtimePath).isSymLink() || runtime.size()>4096 || !runtime.open(QIODevice::ReadOnly))return {};
    return QJsonDocument::fromJson(runtime.readAll()).object();
}
bool AppHost::registered(const QString &sessionPath) {
    // The app runs a private copy, so the plugin folder is found through the registration.
    const QString plugin=runtimeConfig(QDir(QFileInfo(sessionPath).absolutePath()).filePath("runtime.json")).value("plugin").toString();
    const QFileInfo metadata(QDir(plugin).filePath("plugin.json"));
    return QDir::isAbsolutePath(plugin) && metadata.isFile() && !metadata.isSymLink();
}
QString AppHost::replacedStep(bool replaced, bool launcher, bool registered, bool editing) {
    if(!replaced)return "stay";
    if(!launcher)return "quit"; // uninstall removes the launcher; nothing can start or reach a backend
    if(!registered)return "wait";
    return editing?"ask":"restart";
}
bool AppHost::registrationReady(const QString &runtimePath) {
    const QJsonObject config=runtimeConfig(runtimePath);
    const QString expected=config.value("executable_sha256").toString();
    const QString path=QDir(config.value("bundle").toString()).filePath("legiongo2companion");
    QFile executable(path);
    // The backend publishes the private copy before the new SHA-256; both must match before a restart.
    if(expected.size()!=64 || !QDir::isAbsolutePath(config.value("bundle").toString()) || QFileInfo(path).isSymLink()
       || !QFileInfo(path).isExecutable() || !executable.open(QIODevice::ReadOnly))return false;
    QCryptographicHash hash(QCryptographicHash::Sha256);
    return hash.addData(&executable) && QString::fromLatin1(hash.result().toHex())==expected;
}
QString AppHost::launcherPath() const { return QDir(QFileInfo(sessionPath_).absolutePath()).filePath("launch.py"); }
// A new build's backend replaces this private copy, and uninstall deletes it, while it runs
// (/proc/self/exe then ends in " (deleted)"). An update brings a backend the old app was not built for.
void AppHost::checkReplaced() {
#ifdef Q_OS_LINUX
    char target[4096];
    const ssize_t size=readlink("/proc/self/exe",target,sizeof target);
    const bool replaced=size>0 && QByteArray(target,int(size)).endsWith(" (deleted)");
    const QFileInfo launcher(launcherPath());
    const bool present=replaced && launcher.isFile() && !launcher.isSymLink();
    const QString step=replacedStep(replaced,present,present && registrationReady(QDir(launcher.absolutePath()).filePath("runtime.json")),
        window_ && window_->property("editing").toBool());
    if(step=="quit") { CompanionDiagnostics::event("application_removed");emit quitRequested();return; }
    if(step=="restart") { CompanionDiagnostics::event("application_replaced");restartNow();return; }
    if((step=="ask")!=updateWaiting_) { updateWaiting_=step=="ask";emit changed(); }
#endif
}
void AppHost::restartNow() {
    if(capture_)return;
    // main() starts the launcher after the event loop ends and the single-instance lock is released.
    relaunch_=true;emit quitRequested();
}
void AppHost::setWindow(QQuickWindow *window) { window_=window; }
void AppHost::startInTray() {
    // The autostart entry runs only in Plasma, which is Desktop Mode before any RPC confirms it.
    if(!capture_ && !desktop_) { desktop_=true;update(true); }
}
static QByteArray smallFile(const QString &path) {
    QFile file(path);
    return file.size()<=(4<<20) && file.open(QIODevice::ReadOnly)?file.read(4<<20):QByteArray();
}
QString AppHost::steamName(const QString &steamRoot, const QString &appId) {
    bool numeric=false;const quint32 id=appId.toUInt(&numeric);
    if(!numeric || !id)return {};
    // Text VDF: "key"<whitespace>"value", with \\ and \" escapes.
    static const QRegularExpression pair(R"vdf("(\w+)"\s+"((?:[^"\\]|\\.)*)")vdf");
    const auto value=[](QString text){ return text.replace("\\\"","\"").replace("\\\\","\\"); };
    QStringList libraries{steamRoot};
    for(auto it=pair.globalMatch(QString::fromUtf8(smallFile(steamRoot+"/steamapps/libraryfolders.vdf")));it.hasNext();) {
        const auto match=it.next();
        if(match.captured(1)=="path")libraries<<value(match.captured(2));
    }
    for(const auto &library:libraries) {
        for(auto it=pair.globalMatch(QString::fromUtf8(smallFile(library+"/steamapps/appmanifest_"+appId+".acf")));it.hasNext();) {
            const auto match=it.next();
            if(match.captured(1)=="name")return value(match.captured(2));
        }
    }
    // Non-Steam shortcuts: binary VDF entries, each an int32 "appid" followed by its string "AppName".
    static const QByteArray idKey("\x02" "appid\0",7),nameKey("\x01" "appname\0",9);
    for(const auto &user:QDir(steamRoot+"/userdata").entryInfoList(QDir::Dirs|QDir::NoDotAndDotDot)) {
        const QByteArray data=smallFile(user.filePath()+"/config/shortcuts.vdf"),lower=data.toLower();
        for(qsizetype at=lower.indexOf(idKey);at>=0 && at+11<=data.size();at=lower.indexOf(idKey,at+1)) {
            if(qFromLittleEndian<quint32>(data.constData()+at+7)!=id)continue;
            const qsizetype next=lower.indexOf(idKey,at+1),name=lower.indexOf(nameKey,at),end=data.indexOf('\0',name+9);
            if(name>=0 && end>name && (next<0 || name<next))return QString::fromUtf8(data.mid(name+9,end-name-9));
        }
    }
    return {};
}
QString AppHost::gameName(const QString &appId) {
    // shortcut: a name is looked up once per app run, so a game installed meanwhile shows its id until a restart.
    if(!names_.contains(appId))names_.insert(appId,steamName(QDir::homePath()+"/.local/share/Steam",appId));
    return names_.value(appId);
}
void AppHost::setTrayEnabled(bool value) {
    if(capture_ || !desktop_)return;
    settings_.setValue("trayEnabled",value);settings_.sync();update(true);
}
void AppHost::setCloseToTray(bool value) { if(!capture_ && desktop_) { settings_.setValue("closeToTray",value);settings_.sync();emit changed(); } }
void AppHost::setContext(bool fresh, bool gamescope) {
    const bool next=fresh?!gamescope:desktop_; // No guessed mode from a missing RPC sample.
    if(next!=desktop_) { desktop_=next;update(true); }
}
void AppHost::update(bool recheckTray) {
    if(capture_) { emit changed();return; }
    const bool before=installed_, available=trayAvailable_, visible=tray_.isVisible();
    installed_=registered(sessionPath_);
    // A lost tray host must bring a window hidden to the tray back within one or two monitor ticks.
    // A window started hidden for the tray (autostart) also waits for the tray host that way.
    const bool hiddenInTray=window_ && !window_->isVisible() && (visible?closeToTray():desktop_);
    if(recheckTray || !trayCheck_.isValid() || trayCheck_.hasExpired(trayCheckMs(hiddenInTray))) {
        trayAvailable_=!capture_ && QSystemTrayIcon::isSystemTrayAvailable();trayCheck_.start();
    }
    tray_.setVisible(installed_ && desktop_ && trayAvailable_ && trayEnabled());
    CompanionDiagnostics::change("installation",installed_?"present":"missing");
    CompanionDiagnostics::change("tray",tray_.isVisible()?"visible":"hidden");
    if(visible && !tray_.isVisible() && window_ && !window_->isVisible())showWindow();
    if(before && !installed_)showWindow();
    if(before!=installed_ || available!=trayAvailable_ || visible!=tray_.isVisible())emit changed();
    if(!relaunch_)checkReplaced();
}
bool AppHost::hideWindow() {
    if(!window_ || !tray_.isVisible())return false;
    for(auto *child:window_->findChildren<QQuickWindow *>())child->hide();
    window_->hide();return true;
}
void AppHost::showWindow() {
    if(!window_)return;
    // Restoring from the tray keeps the maximized start size.
    if(window_->windowStates() & Qt::WindowMaximized)window_->showMaximized();else window_->showNormal();
    window_->raise();window_->requestActivate();
}
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
