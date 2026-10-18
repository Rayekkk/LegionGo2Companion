// SPDX-License-Identifier: BSD-3-Clause
#include "rpcclient.h"
#include "diagnostics.h"
#include <QApplication>
#include "apphost.h"
#include "logviewer.h"
#include <QLockFile>
#include <QLocalServer>
#include <QLocalSocket>
#include <QDir>
#include <QFileInfo>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQmlError>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QProcess>
#include <QStandardPaths>
#include <QTimer>
#include <QCommandLineParser>
#include <QFontDatabase>
#include <QDebug>

int main(int argc, char **argv) {
    QApplication application(argc,argv);
    application.setQuitOnLastWindowClosed(false);
#ifdef Q_OS_WIN
    if (qEnvironmentVariable("QT_QPA_PLATFORM") == "offscreen")
        QFontDatabase::addApplicationFont(qEnvironmentVariable("WINDIR") + "/Fonts/segoeui.ttf");
#endif
    application.setApplicationName("Legion Go Companion");
    application.setOrganizationName("Rayek");
    application.setDesktopFileName("legiongo2companion");
    QCommandLineParser parser;
    parser.addHelpOption();
    // --session is consumed by QGuiApplication's session-manager option.
    parser.addOption({"backend-session","Private backend session file.","path"});
    parser.addOption({"capture","Save a local visual-test screenshot.","path"});
    parser.addOption({"capture-page","Visual-test page.","page","overview"});
    parser.addOption({"capture-size","Visual-test window size (WIDTHxHEIGHT).","size"});
    parser.addOption({"capture-fullscreen","Visual-test fullscreen fan editor."});
    parser.addOption({"tray","Start in the tray (the Desktop Mode autostart entry)."});
    parser.process(application);
    const auto path = parser.isSet("backend-session") ? parser.value("backend-session") :
        QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation) + "/legiongo2companion/session.json";
    QQuickStyle::setStyle("Basic");
    const bool capture=parser.isSet("capture"), tray=!capture && parser.isSet("tray");
    if(!capture && qEnvironmentVariable("QT_QPA_PLATFORM")!="offscreen") {
        CompanionDiagnostics::configure(QStandardPaths::writableLocation(QStandardPaths::GenericStateLocation)+"/legiongo2companion");
        CompanionDiagnostics::event("application_started");
        QObject::connect(&application,&QCoreApplication::aboutToQuit,[] {
            CompanionDiagnostics::event("application_exiting",{},0,0,0,-1,false);
            CompanionDiagnostics::configure({});
        });
    }
    const QString instance=QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation)+"/legiongo2companion-ui";
    QLockFile lock(instance+".lock");QLocalServer server;
    if(!capture) {
        QDir().mkpath(QFileInfo(instance).absolutePath());
        if(!lock.tryLock(100)) {
            if(tray)return 0; // Already running; a login start must not bring its window up.
            QLocalSocket client;client.connectToServer(instance);
            if(client.waitForConnected(500)) { client.write("show");client.waitForBytesWritten(500);CompanionDiagnostics::event("instance_forwarded");return 0; }
            CompanionDiagnostics::event("instance_unavailable");
            qWarning()<<"Companion is already starting. Try opening it again.";return 1;
        }
        QLocalServer::removeServer(instance);server.setSocketOptions(QLocalServer::UserAccessOption);
        if(!server.listen(instance)) { CompanionDiagnostics::event("instance_listener_failed");return 1; }
    }
    AppHost host(path,capture);
    if(tray && !host.trayEnabled())return 0; // "Show tray icon" off also turns the login start off.
    RpcClient rpc(path);
    LogViewer logs(&rpc);
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("rpc",&rpc);
    engine.rootContext()->setContextProperty("appHost",&host);
    engine.rootContext()->setContextProperty("logViewer",&logs);
    QObject::connect(&engine,&QQmlApplicationEngine::warnings,&application,[](const QList<QQmlError> &warnings) {
        for(const auto &warning:warnings)
            CompanionDiagnostics::event("qml_warning",{},0,0,0,-1,true,warning.url().fileName(),warning.line());
    });
    QObject::connect(&engine,&QQmlApplicationEngine::objectCreationFailed,&application,[] {
        CompanionDiagnostics::event("qml_creation_failed");QCoreApplication::exit(1);
    },Qt::QueuedConnection);
    if(tray)engine.setInitialProperties({{"visible",false}}); // Main.qml shows itself otherwise
    engine.loadFromModule("Companion","Main");
    if (engine.rootObjects().isEmpty()) { CompanionDiagnostics::event("qml_root_unavailable");return 1; }
    auto *window=qobject_cast<QQuickWindow *>(engine.rootObjects().first());
    host.setWindow(window);
    if(tray) { window->setWindowStates(Qt::WindowMaximized);host.startInTray(); } // shown later from the tray
    else if(!capture)window->showMaximized();
    QObject::connect(&server,&QLocalServer::newConnection,&application,[&host,&server]{
        auto *client=server.nextPendingConnection();
        const auto show=[client,&host]{
            if(client->bytesAvailable()<4)return;
            if(client->read(4)=="show")host.showWindow();
            client->disconnectFromServer();
        };
        QObject::connect(client,&QLocalSocket::readyRead,client,show);
        show();
        QObject::connect(client,&QLocalSocket::disconnected,client,&QObject::deleteLater);
        QTimer::singleShot(1000,client,[client]{client->disconnectFromServer();});
    });
    if (parser.isSet("capture")) {
        if (parser.isSet("capture-size")) {
            const auto dimensions=parser.value("capture-size").split('x');
            bool widthOk=false,heightOk=false;
            const int width=dimensions.value(0).toInt(&widthOk);
            const int height=dimensions.value(1).toInt(&heightOk);
            if(dimensions.size()!=2 || !widthOk || !heightOk || width<980 || height<680 || width>3840 || height>2160)return 2;
            window->resize(width,height);
        }
        window->setProperty("page",parser.value("capture-page"));
        if (parser.isSet("capture-fullscreen")) {
            QTimer::singleShot(2000,&application,[window] {
                if (auto *editor=window->findChild<QQuickWindow *>("fanFullscreen")) editor->showFullScreen();
            });
        }
        QTimer::singleShot(4500,&application,[window,&application,&parser] {
            if (!window->property("backendProblem").toString().isEmpty())
                qWarning() << window->property("backendProblem").toString();
            auto *target=parser.isSet("capture-fullscreen")?window->findChild<QQuickWindow *>("fanFullscreen"):window;
            application.exit(target && target->grabWindow().save(parser.value("capture")) ? 0 : 2);
        });
    }
    const int code=application.exec();
    if(host.relaunchRequested()) {
        server.close();lock.unlock();
        // Same command as the .desktop entry; the launcher checks the new registration itself.
        QProcess::startDetached("/usr/bin/python3",{host.launcherPath()});
    }
    return code;
}
