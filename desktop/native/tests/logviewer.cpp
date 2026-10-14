// SPDX-License-Identifier: BSD-3-Clause
#include "../logviewer.h"
#include "../rpcclient.h"
#include <QtTest>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTemporaryDir>
#ifdef Q_OS_UNIX
#include <sys/stat.h>
#include <unistd.h>
#endif

class LogViewerTests : public QObject {
    Q_OBJECT
    static QVariantMap entry(const QString &event,const QString &level="info",const QString &component="fan",
        const QString &source="backend",int minutes=0) {
        return {{"time",QDateTime::currentDateTimeUtc().addSecs(-minutes*60).toString(Qt::ISODateWithMs)},
            {"event",event},{"level",level},{"component",component},{"source",source}};
    }
    static void writeRows(const QString &directory,const QVariantList &rows,const QByteArray &extra={}) {
        QDir().mkpath(directory);QFile file(QDir(directory).filePath("desktop-events.jsonl"));
        QVERIFY(file.open(QIODevice::WriteOnly));
        for(const auto &row:rows)file.write(QJsonDocument::fromVariant(row).toJson(QJsonDocument::Compact)+"\n");
        file.write(extra);file.close();
    }
    static void complete(RpcClient &rpc,LogViewer &viewer,const QVariantList &events,const QString &error={}) {
        const QVariantMap result{{"events",events},{"available_sources",QStringList{"backend","fan"}},
            {"warnings",QStringList()},{"truncated",false}};
        emit rpc.finished(viewer.request_,"desktop_get_logs",result,error);
    }
private slots:
    void filtersOrderSearchAndAge() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");
        writeRows(logs,{entry("desktop_started","info","desktop","desktop",10)});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        viewer.refresh();complete(rpc,viewer,{entry("old_failure","error","wifi","wifi",120),
            entry("sensor_fault","critical","fan","fan",2),entry("recovered","warning","fan","backend",1),
            entry("ordinary_operation","info","tdp","backend",5)});
        QTRY_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),5);QVERIFY(!viewer.stale());
        QCOMPARE(viewer.records().first().toMap().value("event").toString(),QString("recovered"));
        viewer.setLevel("warning");QCOMPARE(viewer.filteredCount(),3);
        viewer.setLevel("error");QCOMPARE(viewer.filteredCount(),2);
        viewer.setAgeMinutes(60);QCOMPARE(viewer.filteredCount(),1);
        viewer.setLevel("all");viewer.setAgeMinutes(0);viewer.setSource("desktop");QCOMPARE(viewer.filteredCount(),1);
        viewer.setSource("all");viewer.setComponent("fan");QCOMPARE(viewer.filteredCount(),2);
        viewer.setSearch("SENSOR_FAULT");QCOMPARE(viewer.filteredCount(),1);
        viewer.setSearch("missing");QCOMPARE(viewer.filteredCount(),0);
        QVERIFY(viewer.sourceOptions().contains("desktop"));QVERIFY(viewer.componentOptions().contains("tdp"));
    }
    void failedRefreshRetainsRowsAndIgnoresUnrelatedReplies() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        viewer.refresh();complete(rpc,viewer,{entry("first")});QTRY_VERIFY(!viewer.loading());
        viewer.refresh();emit rpc.finished(viewer.request_+99,"desktop_get_logs",QVariantMap(),QString());
        QVERIFY(viewer.loading());complete(rpc,viewer,{},"privateRemoteErrorMarker");QTRY_VERIFY(!viewer.loading());
        QCOMPARE(viewer.totalCount(),1);QVERIFY(viewer.stale());QVERIFY(!viewer.error().contains("privateRemoteErrorMarker"));
        QVERIFY(viewer.warnings().contains("backend_logs_unavailable"));
        viewer.refresh();complete(rpc,viewer,{entry("after_recovery")});QTRY_VERIFY(!viewer.loading());
        QVERIFY(!viewer.stale());QVERIFY(viewer.error().isEmpty());
        QCOMPARE(viewer.records().first().toMap().value("event").toString(),QString("after_recovery"));
    }
    void refreshKeepsSelectedMissingFiltersVisible() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        viewer.refresh();complete(rpc,viewer,{entry("fan_failure","error","fan","fan")});QTRY_VERIFY(!viewer.loading());
        viewer.setComponent("fan");viewer.setSource("fan");QCOMPARE(viewer.filteredCount(),1);
        viewer.refresh();const QVariantMap response{{"events",QVariantList{entry("wifi_failure","error","wifi","wifi")}},
            {"available_sources",QStringList{"wifi"}},{"warnings",QStringList()},{"truncated",false}};
        emit rpc.finished(viewer.request_,"desktop_get_logs",response,QString());QTRY_VERIFY(!viewer.loading());
        QCOMPARE(viewer.filteredCount(),0);QCOMPARE(viewer.component(),QString("fan"));QCOMPARE(viewer.source(),QString("fan"));
        QVERIFY(viewer.componentOptions().contains("fan"));QVERIFY(viewer.sourceOptions().contains("fan"));
        viewer.setComponent("all");viewer.setSource("all");QCOMPARE(viewer.filteredCount(),1);
    }
    void failedLocalReadRetainsItsLastCompleteSnapshot() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs"),leaf=QDir(logs).filePath("desktop-events.jsonl");
        writeRows(logs,{entry("desktop_started","info","desktop","desktop")});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        viewer.refresh();complete(rpc,viewer,{});QTRY_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),1);
        QVERIFY(QFile::remove(leaf));QVERIFY(QDir().mkdir(leaf));
        viewer.refresh();complete(rpc,viewer,{});QTRY_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),1);
        QVERIFY(viewer.stale());QVERIFY(viewer.warnings().contains("desktop_log_unsafe"));
        QVERIFY(QDir().rmdir(leaf));writeRows(logs,{entry("desktop_recovered","info","desktop","desktop")});
        viewer.refresh();complete(rpc,viewer,{});QTRY_VERIFY(!viewer.loading());QVERIFY(!viewer.stale());
        QCOMPARE(viewer.records().first().toMap().value("event").toString(),QString("desktop_recovered"));
    }
    void malformedAndUnknownFieldsAreNotReexported() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");
        auto local=entry("desktop_started","info","desktop","desktop");local.insert("payload","privatePayloadMarker");
        local.insert("error",QVariantMap{{"type","OSError"},{"errno",104},{"message","privateExceptionMarker"}});
        writeRows(logs,{local},"not-json\n");
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        auto backend=entry("failed");backend.insert("args","privateArgumentMarker");backend.insert("context",QVariantMap{{"reason","token=privateCredentialMarker"},{"unknown","privateUnknownMarker"}});
        viewer.refresh();complete(rpc,viewer,{backend,QVariantMap{{"event","bad"}}});QTRY_VERIFY(!viewer.loading());
        QCOMPARE(viewer.totalCount(),2);QVERIFY(viewer.warnings().contains("desktop_log_malformed"));
        QVERIFY(viewer.warnings().contains("backend_log_malformed"));
        viewer.exportFiltered();QTRY_VERIFY(!viewer.exporting());QVERIFY2(viewer.error().isEmpty(),qPrintable(viewer.error()));
        QFile exported(viewer.exportedPath());QVERIFY(exported.open(QIODevice::ReadOnly));const auto content=exported.readAll();
        for(const auto &secret:{"privatePayloadMarker","privateExceptionMarker","privateArgumentMarker","privateCredentialMarker","privateUnknownMarker"})QVERIFY(!content.contains(secret));
        QVERIFY(content.contains("OSError"));QVERIFY(content.contains("104"));
    }
    void exportUsesFilteredSnapshotAndPrivatePermissions() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs"),desktop=temp.filePath("Desktop");QVERIFY(QDir().mkpath(desktop));writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,desktop);
        viewer.refresh();complete(rpc,viewer,{entry("keep_failure","error"),entry("skip_success","info")});QTRY_VERIFY(!viewer.loading());
        viewer.setLevel("error");viewer.exportFiltered();viewer.setLevel("all");QTRY_VERIFY(!viewer.exporting());
        const auto path=viewer.exportedPath();QVERIFY(path.startsWith(desktop+"/LegionGoCompanion-logs-"));QVERIFY(path.endsWith(".jsonl"));
        QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));const auto content=file.readAll();
        QCOMPARE(content.count('\n'),1);QVERIFY(content.contains("keep_failure"));QVERIFY(!content.contains("skip_success"));
#ifdef Q_OS_UNIX
        struct stat info {};QCOMPARE(::stat(QFile::encodeName(path).constData(),&info),0);QCOMPARE(info.st_mode&0777,mode_t(0600));
#endif
    }
    void exportCollisionNeverOverwritesAnExistingFile() {
        QTemporaryDir temp;const auto original=temp.filePath("LegionGoCompanion-logs-fixed.jsonl");
        QFile file(original);QVERIFY(file.open(QIODevice::WriteOnly));file.write("preserve");file.close();
        QString error;const auto path=LogViewer::writeSnapshot(temp.path(),"{\"event\":\"new\"}\n","fixed",error);
        QVERIFY2(error.isEmpty(),qPrintable(error));QCOMPARE(QFileInfo(path).fileName(),QString("LegionGoCompanion-logs-fixed-1.jsonl"));
        QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),QByteArray("preserve"));
    }
    void missingDesktopHasNoFallbackAndEmptyFilterDoesNotCreateFiles() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs"),missing=temp.filePath("missing-desktop");writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,missing);
        viewer.refresh();complete(rpc,viewer,{entry("failure","error")});QTRY_VERIFY(!viewer.loading());
        viewer.exportFiltered();QTRY_VERIFY(!viewer.exporting());QVERIFY(viewer.exportedPath().isEmpty());
        QCOMPARE(viewer.error(),QString("Desktop folder is unavailable."));QVERIFY(!QFileInfo::exists(missing));
        viewer.setSearch("not_present");viewer.exportFiltered();QVERIFY(!viewer.exporting());QVERIFY(viewer.error().contains("no matching entries"));
    }
    void fileBoundAndNewestSnapshotLimit() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");QVERIFY(QDir().mkpath(logs));
        QFile file(QDir(logs).filePath("desktop-events.jsonl"));QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(QByteArray(1024*1024,'x'));file.write("\n");
        QVariantList rows;for(int index=0;index<1100;++index)rows.append(entry(QString("record_%1").arg(index),"info","desktop","desktop",index));
        for(const auto &row:rows)file.write(QJsonDocument::fromVariant(row).toJson(QJsonDocument::Compact)+"\n");file.close();
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        viewer.refresh();complete(rpc,viewer,{});QTRY_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),1000);
        QCOMPARE(viewer.records().first().toMap().value("event").toString(),QString("record_0"));
        QVERIFY(viewer.warnings().contains("record_limit_reached"));QVERIFY(viewer.warnings().contains("desktop_log_truncated"));
    }
    void localLinksAndSpecialFilesAreRejected() {
#ifdef Q_OS_UNIX
        QTemporaryDir temp;const auto logs=temp.filePath("logs");QVERIFY(QDir().mkpath(logs));
        const auto outside=temp.filePath("outside"),leaf=QDir(logs).filePath("desktop-events.jsonl");
        QFile file(outside);QVERIFY(file.open(QIODevice::WriteOnly));file.write("preserve");file.close();
        QVERIFY(QFile::link(outside,leaf));
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        viewer.refresh();complete(rpc,viewer,{});QTRY_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),0);QVERIFY(viewer.stale());
        QVERIFY(QFile::remove(leaf));QCOMPARE(::mkfifo(QFile::encodeName(leaf).constData(),0600),0);
        viewer.refresh();complete(rpc,viewer,{});QTRY_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),0);QVERIFY(viewer.stale());
        QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),QByteArray("preserve"));
        QString error;const auto collision=temp.filePath("LegionGoCompanion-logs-link.jsonl");QVERIFY(QFile::link(outside,collision));
        const auto exported=LogViewer::writeSnapshot(temp.path(),"{}\n","link",error);QVERIFY(error.isEmpty());
        QVERIFY(exported.endsWith("-1.jsonl"));
#else
        QSKIP("POSIX no-follow and nonblocking special-file semantics");
#endif
    }
};
QTEST_GUILESS_MAIN(LogViewerTests)
#include "logviewer.moc"
