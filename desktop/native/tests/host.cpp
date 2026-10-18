// SPDX-License-Identifier: BSD-3-Clause
#include "../apphost.h"
#include <QtTest>
#include <QTemporaryDir>
#include <QFile>
#include <QDir>
#include <QCryptographicHash>
#include <QJsonDocument>
#include <QJsonObject>

class HostTests final : public QObject {
    Q_OBJECT
private slots:
    void onlyStoppedEmptyServicePermitsStart() {
        QCOMPARE(AppHost::classifyService("ActiveState=inactive\nMainPID=0\n"),QString("inactive"));
        QCOMPARE(AppHost::classifyService("MainPID=0\nActiveState=failed\n"),QString("failed"));
        QCOMPARE(AppHost::classifyService("ActiveState=failed\nMainPID=123\n"),QString("unknown"));
        QCOMPARE(AppHost::classifyService("ActiveState=active\nMainPID=123\n"),QString("active"));
        for(const auto &data:{"", "ActiveState=inactive\n", "ActiveState=activating\nMainPID=0\n", "ActiveState=inactive\nMainPID=0\nMainPID=1\n"})
            QCOMPARE(AppHost::classifyService(data),QString("unknown"));
    }
    void unloadKeepsRegistrationButUninstallRemovesIt() {
        QTemporaryDir root;QDir directory(root.path());
        QVERIFY(directory.mkpath("plugin"));QVERIFY(directory.mkpath("private"));
        const auto session=root.filePath("private/session.json");
        QFile metadata(root.filePath("plugin/plugin.json"));QVERIFY(metadata.open(QIODevice::WriteOnly));metadata.write("{}");metadata.close();
        QFile runtime(root.filePath("private/runtime.json"));
        const auto record=[&](const QString &plugin){
            QVERIFY(runtime.open(QIODevice::WriteOnly|QIODevice::Truncate));
            runtime.write(QJsonDocument(QJsonObject{{"plugin",plugin}}).toJson());runtime.close();
        };
        record(root.filePath("plugin"));
        QVERIFY(AppHost::registered(session)); // Missing session alone is a recoverable unload/crash.
        QVERIFY(runtime.remove());QVERIFY(!AppHost::registered(session));
        record("plugin");QVERIFY(!AppHost::registered(session)); // relative plugin folder
        record(root.filePath("plugin"));
        QVERIFY(metadata.remove());QVERIFY(!AppHost::registered(session));
    }
    void trayAvailabilityIsRecheckedQuicklyOnlyWhileTheWindowIsHiddenInTheTray() {
        QVERIFY(AppHost::trayCheckMs(true)<=2000); // the monitor ticks every 2 s
        QVERIFY(AppHost::trayCheckMs(false)>=30000);
    }
    void replacedExecutableQuitsAfterUninstallAndRestartsOnlyIntoARegisteredUpdate() {
        QCOMPARE(AppHost::replacedStep(false,true,true,false),QString("stay"));
        QCOMPARE(AppHost::replacedStep(false,false,false,true),QString("stay"));
        QCOMPARE(AppHost::replacedStep(true,false,false,false),QString("quit"));
        QCOMPARE(AppHost::replacedStep(true,false,false,true),QString("quit"));
        QCOMPARE(AppHost::replacedStep(true,true,false,false),QString("wait"));
        QCOMPARE(AppHost::replacedStep(true,true,true,true),QString("ask"));
        QCOMPARE(AppHost::replacedStep(true,true,true,false),QString("restart"));
    }
    void registrationIsReadyOnlyWhenTheRecordedExecutableMatches() {
        QTemporaryDir root;QDir directory(root.path());QVERIFY(directory.mkpath("bundle"));
        QFile executable(root.filePath("bundle/legiongo2companion"));QVERIFY(executable.open(QIODevice::WriteOnly));executable.write("new build");executable.close();
        const QString runtime=root.filePath("runtime.json");
        const auto record=[&](const QString &bundle,const QByteArray &sha){
            QFile file(runtime);QVERIFY(file.open(QIODevice::WriteOnly|QIODevice::Truncate));
            file.write(QJsonDocument(QJsonObject{{"bundle",bundle},{"executable_sha256",QString::fromLatin1(sha)}}).toJson());
        };
        const QByteArray sha=QCryptographicHash::hash("new build",QCryptographicHash::Sha256).toHex();
        QVERIFY(!AppHost::registrationReady(runtime)); // not written yet
        record(root.filePath("bundle"),QCryptographicHash::hash("old build",QCryptographicHash::Sha256).toHex());
        QVERIFY(!AppHost::registrationReady(runtime)); // the old registration: the launcher would refuse
        record(root.filePath("bundle"),sha);QVERIFY(!AppHost::registrationReady(runtime)); // unpacked, not yet executable
        QVERIFY(executable.setPermissions(executable.permissions()|QFileDevice::ExeOwner));
        QVERIFY(AppHost::registrationReady(runtime));
        record("bundle",sha);QVERIFY(!AppHost::registrationReady(runtime)); // relative bundle
        record(root.filePath("missing"),sha);QVERIFY(!AppHost::registrationReady(runtime));
    }
    void noTrayNoHiddenWindowOrRecoveryInCapture() {
        AppHost host("missing",true);QQuickWindow window;host.setWindow(&window);
        window.show();QVERIFY(window.isVisible());
        QVERIFY(!host.hideWindow());QVERIFY(window.isVisible());
        QVERIFY(!host.canStartService());host.startService();QVERIFY(!host.serviceBusy());
        host.setContext(true,true);QVERIFY(!host.desktopMode());QVERIFY(!host.trayVisible());
        host.restartNow();QVERIFY(!host.relaunchRequested());
        host.startInTray();QVERIFY(!host.desktopMode());
    }
    void gameNamesComeFromSteamManifestsLibrariesAndShortcuts() {
        QTemporaryDir root;QDir steam(root.path());
        QVERIFY(steam.mkpath("steamapps"));QVERIFY(steam.mkpath("card/steamapps"));QVERIFY(steam.mkpath("userdata/1/config"));
        const auto write=[&](const QString &path,const QByteArray &data){ QFile file(steam.filePath(path));QVERIFY(file.open(QIODevice::WriteOnly));file.write(data); };
        write("steamapps/libraryfolders.vdf","\"libraryfolders\"\n{\n\t\"1\"\n\t{\n\t\t\"path\"\t\t\""+steam.filePath("card").toUtf8()+"\"\n\t}\n}\n");
        write("steamapps/appmanifest_10.acf","\"AppState\"\n{\n\t\"appid\"\t\t\"10\"\n\t\"name\"\t\t\"Counter-Strike \\\"Classic\\\"\"\n}\n");
        write("card/steamapps/appmanifest_20.acf","\"AppState\"\n{\n\t\"name\"\t\t\"On the card\"\n}\n");
        // Two binary shortcut entries; the first has no name, so its id must not take the next entry's.
        const auto entry=[](quint32 id,const QByteArray &name){
            QByteArray data("\x00" "0\x00",3);data+=QByteArray("\x02" "appid\x00",7);
            for(int i=0;i<4;++i)data+=char((id>>(8*i))&0xff);
            if(!name.isEmpty())data+=QByteArray("\x01" "AppName\x00",9)+name+QByteArray(1,'\0');
            return data+QByteArray(1,'\x08');
        };
        write("userdata/1/config/shortcuts.vdf",QByteArray("\x00" "shortcuts\x00",11)+entry(3000000000u,"")+entry(3000000001u,"Emulator")+"\x08\x08");
        QCOMPARE(AppHost::steamName(root.path(),"10"),QString("Counter-Strike \"Classic\""));
        QCOMPARE(AppHost::steamName(root.path(),"20"),QString("On the card"));
        QCOMPARE(AppHost::steamName(root.path(),"3000000001"),QString("Emulator"));
        QCOMPARE(AppHost::steamName(root.path(),"3000000000"),QString());
        for(const auto &id:{"30","0","","../10","-1"})QCOMPARE(AppHost::steamName(root.path(),id),QString());
    }
};
QTEST_MAIN(HostTests)
#include "host.moc"
