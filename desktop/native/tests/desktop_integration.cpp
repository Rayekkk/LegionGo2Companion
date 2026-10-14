// SPDX-License-Identifier: BSD-3-Clause
// Opt-in KDE test. Only its own windows, tray preferences and temporary files
// are changed. It never issues hardware RPCs or starts/stops Decky.
#include "../apphost.h"
#include <QtTest>
#include <QApplication>
#include <QTemporaryDir>
#include <QDir>
#include <QFile>

class DesktopIntegration final : public QObject {
    Q_OBJECT
private slots:
    void trayHideShowModeAndPreference() {
        QTemporaryDir root;const auto bundle=fixture(root);
        AppHost host(root.filePath("private/session.json"),false,nullptr,bundle);
        QQuickWindow window;window.setTitle("Companion tray validation");window.resize(320,180);window.show();host.setWindow(&window);
        QVERIFY(host.installed());QVERIFY(!host.trayVisible());
        host.setContext(true,false);QVERIFY(host.desktopMode());QTRY_VERIFY(host.trayAvailable());QTRY_VERIFY(host.trayVisible());
        QVERIFY(host.hideWindow());QVERIFY(!window.isVisible());host.showWindow();QTRY_VERIFY(window.isVisible());
        QVERIFY(host.hideWindow());host.setTrayEnabled(false);QVERIFY(!host.trayVisible());QVERIFY(window.isVisible());
        host.setTrayEnabled(true);QVERIFY(host.trayVisible());host.setCloseToTray(true);
        QSettings prefs;QVERIFY(prefs.value("trayEnabled").toBool());QVERIFY(prefs.value("closeToTray").toBool());
        host.setContext(true,true);QVERIFY(!host.trayVisible());
        host.setTrayEnabled(false);QVERIFY(host.trayEnabled()); // Gaming Mode cannot change the preference.
        host.setContext(true,false);QVERIFY(host.trayVisible());
        // Tray behaviour must not depend on a running Decky service. Service
        // admission is covered by HostTests without starting/stopping it here.
    }
    void uninstallRestoresHiddenWindowAndRemovesTray() {
        QTemporaryDir root;const auto bundle=fixture(root);
        AppHost host(root.filePath("private/session.json"),false,nullptr,bundle);
        QQuickWindow window;window.setTitle("Companion uninstall fixture");window.resize(320,180);window.show();host.setWindow(&window);
        host.setContext(true,false);QTRY_VERIFY(host.trayVisible());QVERIFY(host.hideWindow());
        QVERIFY(QFile::remove(root.filePath("private/runtime.json")));
        QTRY_VERIFY_WITH_TIMEOUT(!host.installed(),4000);QVERIFY(window.isVisible());QVERIFY(!host.trayVisible());QVERIFY(!host.canStartService());
    }
private:
    static QString fixture(QTemporaryDir &root) {
        QDir directory(root.path());directory.mkpath("plugin/dist/desktop");directory.mkpath("private");
        for(const auto &name:{"plugin/plugin.json","private/runtime.json"}) { QFile file(root.filePath(name));if(!file.open(QIODevice::WriteOnly))qFatal("Fixture creation failed");file.write("{}"); }
        return root.filePath("plugin/dist/desktop");
    }
};
int main(int argc,char **argv) {
    QApplication app(argc,argv);app.setOrganizationName("CompanionNativeValidation");app.setApplicationName("TrayTest");
    QTemporaryDir preferences;
    if(!preferences.isValid())qFatal("Cannot create isolated tray preferences.");
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QSettings::setPath(QSettings::IniFormat,QSettings::UserScope,preferences.path());
    QSettings::setPath(QSettings::IniFormat,QSettings::SystemScope,preferences.path());
    if(!qEnvironmentVariable("XDG_CURRENT_DESKTOP").contains("KDE"))qFatal("This opt-in test requires an actual KDE Desktop session.");
    DesktopIntegration test;return QTest::qExec(&test,argc,argv);
}
#include "desktop_integration.moc"
