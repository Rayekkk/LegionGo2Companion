// SPDX-License-Identifier: BSD-3-Clause
#include "../rpcclient.h"
#include "../diagnostics.h"
#include "wait.h"
#include <QTemporaryDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTcpServer>
#include <QTcpSocket>

class TransportTests : public QObject {
    Q_OBJECT
private slots:
    void cleanup() { CompanionDiagnostics::configure({}); }
    void diagnosticLogIsPrivateOptInAndCoalesced() {
        QTemporaryDir directory;const auto path=directory.filePath("diagnostics");
        QVERIFY(CompanionDiagnostics::configure(path));
        for(int i=0;i<20;++i)CompanionDiagnostics::event("rpc_failed","get_version",i+1,503,99);
        for(int i=0;i<20;++i)CompanionDiagnostics::change("backend_session","unavailable");
        for(int i=0;i<20;++i)CompanionDiagnostics::change("backend_session","available");
        CompanionDiagnostics::event("rpc_failed","https://private.invalid/?token=privateToken",31,500);
        QFile file(QDir(path).filePath("desktop-events.jsonl"));QVERIFY(file.open(QIODevice::ReadOnly));
        const auto bytes=file.readAll();
        QCOMPARE(bytes.count('\n'),4);
        QVERIFY(!bytes.contains("privateToken"));QVERIFY(!bytes.contains("private.invalid"));
        for(const auto &line:bytes.split('\n'))if(!line.isEmpty()) {
            const auto object=QJsonDocument::fromJson(line).object();
            QVERIFY(object.contains("time"));QVERIFY(object.contains("pid"));QVERIFY(object.contains("session"));
            QCOMPARE(object.value("component").toString(),QString("desktop"));
            QVERIFY(object.value("level")=="warning" || object.value("level")=="info");
        }
#ifdef Q_OS_UNIX
        struct stat info {};QCOMPARE(::stat(QFile::encodeName(path).constData(),&info),0);
        QCOMPARE(info.st_mode&0777,mode_t(0700));
        QCOMPARE(::stat(QFile::encodeName(file.fileName()).constData(),&info),0);
        QCOMPARE(info.st_mode&0777,mode_t(0600));
#endif
        CompanionDiagnostics::configure({});
        const auto size=file.size();CompanionDiagnostics::event("disabled_log");QCOMPARE(file.size(),size);
    }
    void diagnosticRpcFaultAfterRecoveryIsImmediateAndRepeatsStayQuiet() {
        QTemporaryDir directory;const auto path=directory.filePath("diagnostics");
        QVERIFY(CompanionDiagnostics::configure(path));
        for(int i=0;i<20;++i)CompanionDiagnostics::rpc("get_version",i+1,true,false,503,99,5);
        for(int i=0;i<20;++i)CompanionDiagnostics::rpc("get_status",i+21,true,false,503,99,5);
        for(int i=0;i<20;++i)CompanionDiagnostics::rpc("get_version",i+41,false,false,200,0,5);
        for(int i=0;i<20;++i)CompanionDiagnostics::rpc("get_version",i+61,true,false,503,99,5);
        for(int i=0;i<20;++i)CompanionDiagnostics::rpc("get_status",i+81,true,false,503,99,5);
        for(int i=0;i<20;++i)CompanionDiagnostics::rpc("get_version",i+101,false,false,200,0,5);
        QFile file(QDir(path).filePath("desktop-events.jsonl"));QVERIFY(file.open(QIODevice::ReadOnly));
        QStringList versionEvents,statusEvents;
        for(const auto &line:file.readAll().split('\n'))if(!line.isEmpty()) {
            const auto object=QJsonDocument::fromJson(line).object();
            if(object.value("method")=="get_version")versionEvents.append(object.value("event").toString());
            if(object.value("method")=="get_status")statusEvents.append(object.value("event").toString());
        }
        QCOMPARE(versionEvents,QStringList({"rpc_failed","rpc_recovered","rpc_failed","rpc_recovered"}));
        QCOMPARE(statusEvents,QStringList({"rpc_failed"}));
    }
    void diagnosticRotationKeepsTwoBoundedBackups() {
        QTemporaryDir directory;const auto path=directory.filePath("diagnostics");
        QVERIFY(CompanionDiagnostics::configure(path));
        const auto file=QDir(path).filePath("desktop-events.jsonl");
        for(int i=0;i<3;++i) {
            QFile seed(file);QVERIFY(seed.open(QIODevice::WriteOnly|QIODevice::Truncate));
            QVERIFY(seed.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner));
            QCOMPARE(seed.write(QByteArray(int(CompanionDiagnostics::MaxBytes),'x')),CompanionDiagnostics::MaxBytes);
            seed.close();CompanionDiagnostics::event("rotation_check",{},i+1,0,0,-1,false);
        }
        QVERIFY(QFileInfo::exists(file+".1"));QVERIFY(QFileInfo::exists(file+".2"));
        QVERIFY(!QFileInfo::exists(file+".3"));
        for(const auto &name:{file,file+".1",file+".2"})QVERIFY(QFileInfo(name).size()<=CompanionDiagnostics::MaxBytes);
    }
    void unavailableDiagnosticFileCannotBlockRpcFailure() {
        QTemporaryDir directory;const auto path=directory.filePath("diagnostics");
        QVERIFY(CompanionDiagnostics::configure(path));
        QVERIFY(QDir().mkdir(QDir(path).filePath("desktop-events.jsonl")));
        RpcClient client(directory.filePath("missing-session"));QSignalSpy replies(&client,&RpcClient::finished);
        client.call("get_version");WAIT_COMPARE(replies.size(),1);
        QVERIFY(!replies[0][3].toString().isEmpty());
        QVERIFY(QFileInfo(QDir(path).filePath("desktop-events.jsonl")).isDir());
        QVERIFY(QDir().rmdir(QDir(path).filePath("desktop-events.jsonl")));
        client.call("get_version");WAIT_COMPARE(replies.size(),2);
        QVERIFY(!replies[1][3].toString().isEmpty());
        QFile recovered(QDir(path).filePath("desktop-events.jsonl"));QVERIFY(recovered.open(QIODevice::ReadOnly));
        QVERIFY(recovered.readAll().contains("backend_session_unavailable"));
    }
    void diagnosticFileRefusesForeignLinks() {
#ifdef Q_OS_UNIX
        QTemporaryDir directory;const auto path=directory.filePath("diagnostics");
        const auto outside=directory.filePath("outside");QFile victim(outside);
        QVERIFY(victim.open(QIODevice::WriteOnly));victim.write("preserve");victim.close();
        QVERIFY(CompanionDiagnostics::configure(path));const auto leaf=QDir(path).filePath("desktop-events.jsonl");
        QVERIFY(QFile::link(outside,leaf));CompanionDiagnostics::event("symlink_check");
        QVERIFY(QFile::remove(leaf));
        QCOMPARE(::link(QFile::encodeName(outside).constData(),QFile::encodeName(leaf).constData()),0);
        CompanionDiagnostics::event("hardlink_check");
        QVERIFY(victim.open(QIODevice::ReadOnly));QCOMPARE(victim.readAll(),QByteArray("preserve"));
        QVERIFY(QFile::link(path,directory.filePath("linked-directory")));
        QVERIFY(!CompanionDiagnostics::configure(directory.filePath("linked-directory")));
#else
        QSKIP("POSIX no-follow, hardlink and ownership semantics");
#endif
    }
    void transportDiagnosticsExcludeRequestAndResponsePayloads() {
        QTcpServer server;QVERIFY(server.listen(QHostAddress::LocalHost));QTemporaryDir directory;
        const auto path=directory.filePath("session.json"),logs=directory.filePath("diagnostics");
        QFile file(path);QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(QJsonDocument(QJsonObject{{"origin",QString("http://127.0.0.1:%1").arg(server.serverPort())},{"token",QString(43,'a')}}).toJson());
        file.close();file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
        QVERIFY(CompanionDiagnostics::configure(logs));
        connect(&server,&QTcpServer::newConnection,this,[&] {
            auto *socket=server.nextPendingConnection();connect(socket,&QTcpSocket::readyRead,this,[socket] {
                socket->readAll();if(socket->property("answered").toBool())return;socket->setProperty("answered",true);
                const QByteArray body("{\"result\":{\"success\":false,\"error\":\"privateResponseMarker\"}}");
                socket->write("HTTP/1.1 200 OK\r\nContent-Length: "+QByteArray::number(body.size())+"\r\nConnection: close\r\n\r\n"+body);socket->disconnectFromHost();
            });connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
        });
        RpcClient client(path);QSignalSpy replies(&client,&RpcClient::finished);
        client.call("fan_set_enabled",{QString("privateArgumentMarker")},true);WAIT_COMPARE(replies.size(),1);
        QCOMPARE(replies[0][3].toString(),QString("privateResponseMarker"));
        QFile log(QDir(logs).filePath("desktop-events.jsonl"));QVERIFY(log.open(QIODevice::ReadOnly));const auto bytes=log.readAll();
        QVERIFY(bytes.contains("rpc_failed"));QVERIFY(bytes.contains("fan_set_enabled"));
        QVERIFY(!bytes.contains("privateArgumentMarker"));QVERIFY(!bytes.contains("privateResponseMarker"));
        QVERIFY(!bytes.contains(QByteArray(43,'a')));QVERIFY(!bytes.contains("127.0.0.1"));
    }
    void failureWithoutMessageStillFails_data() {
        QTest::addColumn<QByteArray>("body");
        QTest::addColumn<int>("status");
        QTest::addColumn<bool>("failed");
        QTest::newRow("empty-http-error") << QByteArray("{\"error\":\"\"}") << 409 << true;
        QTest::newRow("null-http-error") << QByteArray("{\"error\":null}") << 409 << true;
        QTest::newRow("empty-operation-error") << QByteArray("{\"result\":{\"success\":false,\"error\":\"\"}}") << 200 << true;
        QTest::newRow("reason") << QByteArray("{\"result\":{\"success\":false,\"reason\":\"Busy\"}}") << 200 << true;
        QTest::newRow("no-message") << QByteArray("{\"result\":{\"success\":false}}") << 200 << true;
        QTest::newRow("success-empty-error") << QByteArray("{\"result\":{\"success\":true,\"error\":\"\"}}") << 200 << false;
        QTest::newRow("success") << QByteArray("{\"result\":{\"success\":true}}") << 200 << false;
    }
    void failureWithoutMessageStillFails() {
        QFETCH(QByteArray,body); QFETCH(int,status); QFETCH(bool,failed);
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost));
        QTemporaryDir directory; const auto path=directory.filePath("session.json");
        QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(QJsonDocument(QJsonObject{{"origin",QString("http://127.0.0.1:%1").arg(server.serverPort())},{"token",QString(43,'a')}}).toJson());
        file.close(); file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
        connect(&server,&QTcpServer::newConnection,this,[&] {
            auto *socket=server.nextPendingConnection();
            connect(socket,&QTcpSocket::readyRead,this,[&,socket] {
                const auto input=socket->property("input").toByteArray()+socket->readAll();
                socket->setProperty("input",input);
                if(!input.contains("\r\n\r\n") || socket->property("answered").toBool())return;
                socket->setProperty("answered",true);
                socket->write("HTTP/1.1 "+QByteArray::number(status)+" Response\r\nContent-Length: "+QByteArray::number(body.size())+"\r\nConnection: close\r\n\r\n"+body);
                socket->disconnectFromHost();
            });
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
        });
        RpcClient client(path); QSignalSpy replies(&client,&RpcClient::finished);
        client.call("fan_set_enabled",{false},true); WAIT_COMPARE(replies.size(),1);
        QCOMPARE(!replies[0][3].toString().isEmpty(),failed);
    }
    void sessionValidation() {
        QTemporaryDir directory;
        const auto path=directory.filePath("session.json");
        const auto write=[&](QString origin,QString token) {
            QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly));
            file.write(QJsonDocument(QJsonObject{{"origin",origin},{"token",token}}).toJson());
            file.close(); file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
        };
        QString origin,error; QByteArray token;
        write("http://127.0.0.1:41000",QString(43,'a'));
        QVERIFY(RpcClient::readSession(path,origin,token,error));
        for(const auto &value:{"http://example.invalid:80","http://127.0.0.1:0","http://127.0.0.1:65536","http://127.0.0.1:10/x"}) {
            write(value,QString(43,'a')); QVERIFY(!RpcClient::readSession(path,origin,token,error));
        }
        write("http://127.0.0.1:41000","short"); QVERIFY(!RpcClient::readSession(path,origin,token,error));
    }
    void admissionAndQueue() {
        RpcClient client("missing"); QSignalSpy replies(&client,&RpcClient::finished);
        client.call("set_active_app",{"42"},true);
        client.call("fan_set_enabled",{true},true);
        WAIT_COMPARE(replies.size(),2);
        QVERIFY(!replies[0][3].toString().isEmpty());
        QVERIFY(!replies[1][3].toString().isEmpty());
        QVERIFY(client.monotonicSeconds()>=0);
    }
    void authenticatedRequestAndNoRedirect() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost));
        QTemporaryDir directory; const auto path=directory.filePath("session.json");
        const auto origin=QString("http://127.0.0.1:%1").arg(server.serverPort());
        QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(QJsonDocument(QJsonObject{{"origin",origin},{"token",QString(43,'a')}}).toJson());
        file.close(); file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
        QByteArray request;
        connect(&server,&QTcpServer::newConnection,this,[&] {
            auto socket=server.nextPendingConnection();
            connect(socket,&QTcpSocket::readyRead,this,[&,socket] {
                request.append(socket->readAll());
                if (!request.contains("\r\n\r\n") || !request.contains("get_version")) return;
                socket->write("HTTP/1.1 307 Redirect\r\nLocation: http://example.invalid/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                socket->disconnectFromHost();
            });
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
        });
        RpcClient client(path); QSignalSpy replies(&client,&RpcClient::finished);
        client.call("get_version"); WAIT_COMPARE(replies.size(),1);
        QVERIFY(request.startsWith("POST /rpc HTTP/1.1"));
        // Header names are case-insensitive; Qt 6.9 sends them lower-cased.
        QVERIFY(request.toLower().contains("authorization: bearer " + QByteArray(43,'a')));
        QVERIFY(request.toLower().contains("origin: " + origin.toUtf8()));
        QCOMPARE(replies[0][3].toString(),QString("Backend redirect rejected."));
    }
    void truncatedHttpCannotConfirmMutationEvenWithValidJson() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost));
        QTemporaryDir directory; const auto path=directory.filePath("session.json");
        const auto origin=QString("http://127.0.0.1:%1").arg(server.serverPort());
        QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(QJsonDocument(QJsonObject{{"origin",origin},{"token",QString(43,'a')}}).toJson());
        file.close(); file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
        connect(&server,&QTcpServer::newConnection,this,[&] {
            auto socket=server.nextPendingConnection();
            connect(socket,&QTcpSocket::readyRead,this,[socket] {
                socket->readAll();
                if (socket->property("answered").toBool()) return;
                socket->setProperty("answered",true);
                socket->write("HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{\"result\":true}");
                socket->disconnectFromHost();
            });
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
        });
        RpcClient client(path); QSignalSpy replies(&client,&RpcClient::finished);
        client.call("fan_set_enabled",{false},true); WAIT_COMPARE(replies.size(),1);
        QCOMPARE(replies[0][3].toString(),QString("Connection lost. Check the value, then try again."));
    }
    void hungVersionReadIsBoundedAndFreshSessionReconnects() {
        QTcpServer server;QVERIFY(server.listen(QHostAddress::LocalHost));QTemporaryDir directory;
        const auto path=directory.filePath("session.json");
        const auto write=[&](const QString &token) {
            QFile file(path);QVERIFY(file.open(QIODevice::WriteOnly));
            file.write(QJsonDocument(QJsonObject{{"origin",QString("http://127.0.0.1:%1").arg(server.serverPort())},{"token",token}}).toJson());
            file.close();file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
        };
        int requests=0;QByteArray observed;
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection();
            connect(socket,&QTcpSocket::readyRead,this,[&,socket]{
                const auto bytes=socket->readAll();
                if(socket->property("seen").toBool())return;
                socket->setProperty("seen",true);++requests;observed=bytes;
                if(requests==1)return; // Silent server, no actual Decky process is stopped.
                socket->write("HTTP/1.1 200 OK\r\nContent-Length: 30\r\nConnection: close\r\n\r\n{\"result\":{\"version\":\"2.1.0\"}}");socket->disconnectFromHost();
            });
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
        });
        write(QString(43,'a'));RpcClient client(path);QSignalSpy replies(&client,&RpcClient::finished);
        QElapsedTimer time;time.start();client.call("get_version");WAIT_COMPARE_T(replies.size(),1,6000);
        QVERIFY(time.elapsed()<6000);QVERIFY(!replies[0][3].toString().isEmpty());
        write(QString(43,'b'));client.call("get_version");WAIT_COMPARE(replies.size(),2);
        QCOMPARE(requests,2);QVERIFY(observed.contains("Bearer "+QByteArray(43,'b')));
        QCOMPARE(replies[1][3].toString(),QString());
    }
    void closingAbortsTheRunningReadButNotWrites() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost));
        QTemporaryDir directory; const auto path=directory.filePath("session.json");
        QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(QJsonDocument(QJsonObject{{"origin",QString("http://127.0.0.1:%1").arg(server.serverPort())},{"token",QString(43,'a')}}).toJson());
        file.close(); file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
        int requests=0; // Silent server: every request hangs until the client gives up.
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection();
            connect(socket,&QTcpSocket::readyRead,this,[&,socket]{ socket->readAll(); if(!socket->property("seen").toBool()){socket->setProperty("seen",true);++requests;} });
        });
        RpcClient client(path); QSignalSpy replies(&client,&RpcClient::finished);
        client.call("get_settings"); WAIT_COMPARE(requests,1);
        client.cancelReads(); QCOMPARE(replies.size(),0); // Navigation leaves the running read alone.
        QElapsedTimer time; time.start(); client.cancelReads(true);
        WAIT_COMPARE(replies.size(),1); QVERIFY(time.elapsed()<1000);
        QCOMPARE(replies[0][3].toString(),QString("Obsolete read.")); QCOMPARE(client.pending(),0);
        client.call("fan_set_enabled",{false},true); WAIT_COMPARE(requests,2);
        client.cancelReads(true); QTest::qWait(200); QCOMPARE(replies.size(),1); QCOMPARE(client.pending(),1);
        for(auto *socket:server.findChildren<QTcpSocket *>())socket->abort();
        WAIT_COMPARE(replies.size(),2); QVERIFY(!replies[1][3].toString().isEmpty());
    }
};
QTEST_GUILESS_MAIN(TransportTests)
#include "transport.moc"
