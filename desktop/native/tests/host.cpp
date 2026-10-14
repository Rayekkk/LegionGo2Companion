// SPDX-License-Identifier: BSD-3-Clause
#include "../apphost.h"
#include <QtTest>
#include <QTemporaryDir>
#include <QFile>
#include <QDir>

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
        QVERIFY(directory.mkpath("plugin/dist/desktop"));QVERIFY(directory.mkpath("private"));
        const auto bundle=root.filePath("plugin/dist/desktop"),session=root.filePath("private/session.json");
        QFile metadata(root.filePath("plugin/plugin.json"));QVERIFY(metadata.open(QIODevice::WriteOnly));metadata.write("{}");metadata.close();
        QFile runtime(root.filePath("private/runtime.json"));QVERIFY(runtime.open(QIODevice::WriteOnly));runtime.write("{}");runtime.close();
        QVERIFY(AppHost::registered(bundle,session)); // Missing session alone is a recoverable unload/crash.
        QVERIFY(runtime.remove());QVERIFY(!AppHost::registered(bundle,session));
        QVERIFY(runtime.open(QIODevice::WriteOnly));runtime.close();
        QVERIFY(metadata.remove());QVERIFY(!AppHost::registered(bundle,session));
    }
    void noTrayNoHiddenWindowOrRecoveryInCapture() {
        AppHost host("missing",true);QQuickWindow window;host.setWindow(&window);
        window.show();QVERIFY(window.isVisible());
        QVERIFY(!host.hideWindow());QVERIFY(window.isVisible());
        QVERIFY(!host.canStartService());host.startService();QVERIFY(!host.serviceBusy());
        host.setContext(true,true);QVERIFY(!host.desktopMode());QVERIFY(!host.trayVisible());
    }
};
QTEST_MAIN(HostTests)
#include "host.moc"
