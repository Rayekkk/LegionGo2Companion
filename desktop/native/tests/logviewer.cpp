// SPDX-License-Identifier: BSD-3-Clause
#include "../diagnostics.h"
#include "../logviewer.h"
#include "../rpcclient.h"
#include "wait.h"
#include <QClipboard>
#include <QDir>
#include <QGuiApplication>
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
    static QVariantMap plugin(const QString &event,const QString &session,const QString &level="info",int minutes=0,
        const QVariantMap &context={}) {
        auto row=entry(event,level,"plugin","backend",minutes);row.insert("session",session);
        if(!context.isEmpty())row.insert("context",context);
        return row;
    }
    static QList<QJsonObject> exportedLines(const QString &path) {
        QFile file(path);QList<QJsonObject> lines;
        if(!file.open(QIODevice::ReadOnly))return lines;
        for(const auto &line:file.readAll().split('\n'))if(!line.isEmpty())lines.append(QJsonDocument::fromJson(line).object());
        return lines;
    }
private slots:
    void showsProblemsByDefault() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        QCOMPARE(viewer.level(),QString("warning"));
        viewer.refresh();complete(rpc,viewer,{entry("fine"),entry("careful","warning"),entry("broken","error")});
        WAIT_VERIFY(!viewer.loading());
        QCOMPARE(viewer.totalCount(),3);QCOMPARE(viewer.filteredCount(),2);
        viewer.setLevel("all");QCOMPARE(viewer.filteredCount(),3);
    }
    void exportAlwaysCarriesTheCurrentStartupRecords() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs"),desktop=temp.filePath("Desktop");QVERIFY(QDir().mkpath(desktop));writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,desktop);
        viewer.refresh();complete(rpc,viewer,{
            plugin("startup","old","info",30,{{"version","2.1.9"}}),plugin("runtime","old","info",30,{{"product","OldModel"}}),
            plugin("startup","now","info",10,{{"version","2.2.0"}}),plugin("runtime","now","info",9,{{"product","83N0"},{"bios","RRCN16WW"}}),
            plugin("startup_complete","now","info",8),entry("broken","error","fan","fan",1)});
        WAIT_VERIFY(!viewer.loading());
        QCOMPARE(viewer.filteredCount(),1);QVERIFY(viewer.exportable());
        viewer.exportFiltered();WAIT_VERIFY(!viewer.exporting());QVERIFY2(viewer.error().isEmpty(),qPrintable(viewer.error()));
        const auto lines=exportedLines(viewer.exportedPath());
        QCOMPARE(lines.size(),5);QCOMPARE(lines.at(0).value("type").toString(),QString("export_header"));
        QCOMPARE(lines.at(0).value("filters").toObject().value("level").toString(),QString("warning"));
        QStringList events;for(int i=1;i<lines.size();++i)events.append(lines.at(i).value("event").toString());
        QCOMPARE(events,(QStringList{"startup","runtime","startup_complete","broken"}));
        QCOMPARE(lines.at(2).value("context").toObject().value("bios").toString(),QString("RRCN16WW"));
        QFile exported(viewer.exportedPath());QVERIFY(exported.open(QIODevice::ReadOnly));QVERIFY(!exported.readAll().contains("OldModel"));
        viewer.setLevel("all");viewer.exportFiltered();WAIT_VERIFY(!viewer.exporting());
        int completes=0;for(const auto &line:exportedLines(viewer.exportedPath()))completes+=line.value("event").toString()=="startup_complete";
        QCOMPARE(completes,1);
    }
    void exportIsAvailableForStartupRecordsAloneButNotForNothing() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs"),desktop=temp.filePath("Desktop");QVERIFY(QDir().mkpath(desktop));writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,desktop);
        viewer.refresh();complete(rpc,viewer,{plugin("startup","now"),entry("fine")});WAIT_VERIFY(!viewer.loading());
        QCOMPARE(viewer.filteredCount(),0);QVERIFY(viewer.exportable());
        viewer.exportFiltered();WAIT_VERIFY(!viewer.exporting());QCOMPARE(exportedLines(viewer.exportedPath()).size(),2);
        viewer.refresh();complete(rpc,viewer,{entry("fine")});WAIT_VERIFY(!viewer.loading());
        QVERIFY(!viewer.exportable());viewer.exportFiltered();QVERIFY(viewer.error().contains("no matching entries"));
    }
    void copyPutsTheSameReportOnTheClipboard() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        viewer.refresh();complete(rpc,viewer,{plugin("startup","now"),entry("broken","error")});WAIT_VERIFY(!viewer.loading());
        QGuiApplication::clipboard()->clear();
        viewer.copyToClipboard();QVERIFY2(viewer.error().isEmpty(),qPrintable(viewer.error()));QVERIFY(viewer.copied());
        const auto text=QGuiApplication::clipboard()->text();
        QVERIFY(text.contains("export_header"));QVERIFY(text.contains("\"event\":\"startup\""));QVERIFY(text.contains("broken"));
        viewer.setLevel("all");QVERIFY(!viewer.copied());
    }
    void reportsAnUnavailableFileSinkOnlyForTheCurrentSession() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());
        auto sink=[](const QString &session,int minutes) {
            auto row=entry("file_sink_unavailable","warning","diagnostics","backend",minutes);row.insert("session",session);return row;
        };
        viewer.refresh();complete(rpc,viewer,{plugin("startup","old","info",60),sink("old",60),plugin("startup","now","info",5)});
        WAIT_VERIFY(!viewer.loading());QVERIFY(!viewer.fileSinkUnavailable());
        viewer.refresh();complete(rpc,viewer,{plugin("startup","old","info",60),sink("old",60),plugin("startup","now","info",5),sink("now",5)});
        WAIT_VERIFY(!viewer.loading());QVERIFY(viewer.fileSinkUnavailable());
    }
    void panelLeaseCompletionsAreNotLoggedButFailuresAre() {
        QTemporaryDir temp;const auto state=temp.filePath("state");QVERIFY(CompanionDiagnostics::configure(state));
        CompanionDiagnostics::rpc("set_panel_active",1,false,true,200,0,5);
        CompanionDiagnostics::rpc("set_tdp_limits",2,false,true,200,0,5);
        CompanionDiagnostics::rpc("set_panel_active",3,true,true,503,99,5);
        CompanionDiagnostics::configure({});
        QFile file(QDir(state).filePath("desktop-events.jsonl"));QVERIFY(file.open(QIODevice::ReadOnly));const auto content=file.readAll();
        QCOMPARE(content.count("rpc_completed"),1);QVERIFY(content.contains("set_tdp_limits"));
        QCOMPARE(content.count("set_panel_active"),1);QVERIFY(content.contains("rpc_failed"));
    }
    void filtersOrderSearchAndAge() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");
        writeRows(logs,{entry("desktop_started","info","desktop","desktop",10)});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());viewer.setLevel("all");
        viewer.refresh();complete(rpc,viewer,{entry("old_failure","error","wifi","wifi",120),
            entry("sensor_fault","critical","fan","fan",2),entry("recovered","warning","fan","backend",1),
            entry("ordinary_operation","info","tdp","backend",5)});
        WAIT_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),5);QVERIFY(!viewer.stale());
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
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());viewer.setLevel("all");
        viewer.refresh();complete(rpc,viewer,{entry("first")});WAIT_VERIFY(!viewer.loading());
        viewer.refresh();emit rpc.finished(viewer.request_+99,"desktop_get_logs",QVariantMap(),QString());
        QVERIFY(viewer.loading());complete(rpc,viewer,{},"privateRemoteErrorMarker");WAIT_VERIFY(!viewer.loading());
        QCOMPARE(viewer.totalCount(),1);QVERIFY(viewer.stale());QVERIFY(!viewer.error().contains("privateRemoteErrorMarker"));
        QVERIFY(viewer.warnings().contains("backend_logs_unavailable"));
        viewer.refresh();complete(rpc,viewer,{entry("after_recovery")});WAIT_VERIFY(!viewer.loading());
        QVERIFY(!viewer.stale());QVERIFY(viewer.error().isEmpty());
        QCOMPARE(viewer.records().first().toMap().value("event").toString(),QString("after_recovery"));
    }
    void refreshKeepsSelectedMissingFiltersVisible() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());viewer.setLevel("all");
        viewer.refresh();complete(rpc,viewer,{entry("fan_failure","error","fan","fan")});WAIT_VERIFY(!viewer.loading());
        viewer.setComponent("fan");viewer.setSource("fan");QCOMPARE(viewer.filteredCount(),1);
        viewer.refresh();const QVariantMap response{{"events",QVariantList{entry("wifi_failure","error","wifi","wifi")}},
            {"available_sources",QStringList{"wifi"}},{"warnings",QStringList()},{"truncated",false}};
        emit rpc.finished(viewer.request_,"desktop_get_logs",response,QString());WAIT_VERIFY(!viewer.loading());
        QCOMPARE(viewer.filteredCount(),0);QCOMPARE(viewer.component(),QString("fan"));QCOMPARE(viewer.source(),QString("fan"));
        QVERIFY(viewer.componentOptions().contains("fan"));QVERIFY(viewer.sourceOptions().contains("fan"));
        viewer.setComponent("all");viewer.setSource("all");QCOMPARE(viewer.filteredCount(),1);
    }
    void failedLocalReadRetainsItsLastCompleteSnapshot() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs"),leaf=QDir(logs).filePath("desktop-events.jsonl");
        writeRows(logs,{entry("desktop_started","info","desktop","desktop")});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());viewer.setLevel("all");
        viewer.refresh();complete(rpc,viewer,{});WAIT_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),1);
        QVERIFY(QFile::remove(leaf));QVERIFY(QDir().mkdir(leaf));
        viewer.refresh();complete(rpc,viewer,{});WAIT_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),1);
        QVERIFY(viewer.stale());QVERIFY(viewer.warnings().contains("desktop_log_unsafe"));
        QVERIFY(QDir().rmdir(leaf));writeRows(logs,{entry("desktop_recovered","info","desktop","desktop")});
        viewer.refresh();complete(rpc,viewer,{});WAIT_VERIFY(!viewer.loading());QVERIFY(!viewer.stale());
        QCOMPARE(viewer.records().first().toMap().value("event").toString(),QString("desktop_recovered"));
    }
    void malformedAndUnknownFieldsAreNotReexported() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");
        auto local=entry("desktop_started","info","desktop","desktop");local.insert("payload","privatePayloadMarker");
        local.insert("error",QVariantMap{{"type","OSError"},{"errno",104},{"message","privateExceptionMarker"}});
        writeRows(logs,{local},"not-json\n");
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());viewer.setLevel("all");
        // Backend rows arrive sanitized (diagnostics_reader); only their known fields are kept.
        auto backend=entry("failed");backend.insert("args","privateArgumentMarker");
        backend.insert("context",QVariantMap{{"reason","timeout"}});backend.insert("error",QVariantMap{{"type","OSError"},{"errno",104}});
        viewer.refresh();complete(rpc,viewer,{backend,QVariantMap{{"event","bad"}}});WAIT_VERIFY(!viewer.loading());
        QCOMPARE(viewer.totalCount(),2);QVERIFY(viewer.warnings().contains("desktop_log_malformed"));
        QVERIFY(viewer.warnings().contains("backend_log_malformed"));
        viewer.exportFiltered();WAIT_VERIFY(!viewer.exporting());QVERIFY2(viewer.error().isEmpty(),qPrintable(viewer.error()));
        QFile exported(viewer.exportedPath());QVERIFY(exported.open(QIODevice::ReadOnly));const auto content=exported.readAll();
        for(const auto &secret:{"privatePayloadMarker","privateExceptionMarker","privateArgumentMarker"})QVERIFY(!content.contains(secret));
        QVERIFY(content.contains("timeout"));QVERIFY(content.contains("OSError"));QVERIFY(content.contains("104"));
    }
    void exportUsesFilteredSnapshotAndPrivatePermissions() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs"),desktop=temp.filePath("Desktop");QVERIFY(QDir().mkpath(desktop));writeRows(logs,{});
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,desktop);viewer.setLevel("all");
        viewer.refresh();complete(rpc,viewer,{entry("keep_failure","error"),entry("skip_success","info")});WAIT_VERIFY(!viewer.loading());
        viewer.setLevel("error");viewer.exportFiltered();viewer.setLevel("all");WAIT_VERIFY(!viewer.exporting());
        const auto path=viewer.exportedPath();QVERIFY(path.startsWith(desktop+"/LegionGoCompanion-logs-"));QVERIFY(path.endsWith(".jsonl"));
        QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));const auto content=file.readAll();
        QCOMPARE(content.count('\n'),2);QVERIFY(content.contains("export_header"));QVERIFY(content.contains("keep_failure"));QVERIFY(!content.contains("skip_success"));
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
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,missing);viewer.setLevel("all");
        viewer.refresh();complete(rpc,viewer,{entry("failure","error")});WAIT_VERIFY(!viewer.loading());
        viewer.exportFiltered();WAIT_VERIFY(!viewer.exporting());QVERIFY(viewer.exportedPath().isEmpty());
        QCOMPARE(viewer.error(),QString("Desktop folder is unavailable."));QVERIFY(!QFileInfo::exists(missing));
        viewer.setSearch("not_present");viewer.exportFiltered();QVERIFY(!viewer.exporting());QVERIFY(viewer.error().contains("no matching entries"));
    }
    void fileBoundAndNewestSnapshotLimit() {
        QTemporaryDir temp;const auto logs=temp.filePath("logs");QVERIFY(QDir().mkpath(logs));
        QFile file(QDir(logs).filePath("desktop-events.jsonl"));QVERIFY(file.open(QIODevice::WriteOnly));
        file.write(QByteArray(1024*1024,'x'));file.write("\n");
        QVariantList rows;for(int index=0;index<1100;++index)rows.append(entry(QString("record_%1").arg(index),"info","desktop","desktop",index));
        for(const auto &row:rows)file.write(QJsonDocument::fromVariant(row).toJson(QJsonDocument::Compact)+"\n");file.close();
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());viewer.setLevel("all");
        viewer.refresh();complete(rpc,viewer,{});WAIT_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),1000);
        QCOMPARE(viewer.records().first().toMap().value("event").toString(),QString("record_0"));
        QVERIFY(viewer.warnings().contains("record_limit_reached"));QVERIFY(viewer.warnings().contains("desktop_log_truncated"));
    }
    void localLinksAndSpecialFilesAreRejected() {
#ifdef Q_OS_UNIX
        QTemporaryDir temp;const auto logs=temp.filePath("logs");QVERIFY(QDir().mkpath(logs));
        const auto outside=temp.filePath("outside"),leaf=QDir(logs).filePath("desktop-events.jsonl");
        QFile file(outside);QVERIFY(file.open(QIODevice::WriteOnly));file.write("preserve");file.close();
        QVERIFY(QFile::link(outside,leaf));
        RpcClient rpc(temp.filePath("missing-session"));LogViewer viewer(&rpc,logs,temp.path());viewer.setLevel("all");
        viewer.refresh();complete(rpc,viewer,{});WAIT_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),0);QVERIFY(viewer.stale());
        QVERIFY(QFile::remove(leaf));QCOMPARE(::mkfifo(QFile::encodeName(leaf).constData(),0600),0);
        viewer.refresh();complete(rpc,viewer,{});WAIT_VERIFY(!viewer.loading());QCOMPARE(viewer.totalCount(),0);QVERIFY(viewer.stale());
        QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),QByteArray("preserve"));
        QString error;const auto collision=temp.filePath("LegionGoCompanion-logs-link.jsonl");QVERIFY(QFile::link(outside,collision));
        const auto exported=LogViewer::writeSnapshot(temp.path(),"{}\n","link",error);QVERIFY(error.isEmpty());
        QVERIFY(exported.endsWith("-1.jsonl"));
#else
        QSKIP("POSIX no-follow and nonblocking special-file semantics");
#endif
    }
};
QTEST_MAIN(LogViewerTests)
#include "logviewer.moc"
