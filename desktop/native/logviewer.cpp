// SPDX-License-Identifier: BSD-3-Clause
#include "logviewer.h"
#include "rpcclient.h"
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QThread>
#include <algorithm>
#include <cmath>
#include <memory>
#ifdef Q_OS_UNIX
#include <cerrno>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace {
constexpr qint64 FileBytes = 1024 * 1024;
constexpr int RecordLimit = 1000;
QString fixedName(const QVariant &value, int limit = 80) {
    const auto text = value.toString();
    static const QRegularExpression syntax("^[A-Za-z0-9_.-]+$");
    return !text.isEmpty() && text.size() <= limit && syntax.match(text).hasMatch() ? text : QString();
}
QString safeText(QString text) {
    text = text.left(4096);
    static const QRegularExpression url("https?://[^\\s<>\"']+", QRegularExpression::CaseInsensitiveOption);
    static const QRegularExpression secret("\\b(password|passwd|psk|token|secret|authorization|cookie|ssid|serial|uuid|app_?id)\\b\\s*[:=]\\s*[^\\r\\n]+", QRegularExpression::CaseInsensitiveOption);
    static const QRegularExpression bearer("Bearer\\s+\\S+", QRegularExpression::CaseInsensitiveOption);
    static const QRegularExpression path("(?:[A-Za-z]:[\\\\/]|/(?:home|Users|root)/)[^\\s\"']+");
    static const QRegularExpression address("\\b(?:[0-9]{1,3}\\.){3}[0-9]{1,3}\\b");
    static const QRegularExpression control("[\\x00-\\x1f\\x7f-\\x9f]");
    text.replace(url,"[url]"); text.replace(secret,"\\1=[redacted]");
    text.replace(bearer,"Bearer [redacted]"); text.replace(path,"[private-path]");
    text.replace(address,"[address]"); text.replace(control," ");
    return text.left(256);
}
QDateTime timestamp(const QVariant &value) {
    const auto text = value.toString();
    if(text.size()>48 || (!text.endsWith('Z') && !text.endsWith("+00:00"))) return {};
    auto result=QDateTime::fromString(text,Qt::ISODateWithMs);
    if(!result.isValid())result=QDateTime::fromString(text,Qt::ISODate);
    return result.isValid()?result.toUTC():QDateTime();
}
bool number(const QVariant &value) {
    const auto type=value.metaType().id();
    return type==QMetaType::Int || type==QMetaType::UInt || type==QMetaType::LongLong
        || type==QMetaType::ULongLong || (type==QMetaType::Double && std::isfinite(value.toDouble()));
}
QVariantMap errorMetadata(const QVariant &value) {
    const auto input=value.toMap();QVariantMap output;
    const auto type=fixedName(input.value("type"));if(!type.isEmpty())output.insert("type",type);
    if(number(input.value("errno")))output.insert("errno",input.value("errno"));
    QVariantList frames;
    for(const auto &item:input.value("frames").toList().mid(0,8)) {
        const auto frame=item.toMap();QVariantMap safe;
        const auto file=fixedName(frame.value("file")),function=fixedName(frame.value("function"));
        if(!file.isEmpty())safe.insert("file",file);
        if(!function.isEmpty())safe.insert("function",function);
        if(number(frame.value("line")))safe.insert("line",frame.value("line"));
        if(!safe.isEmpty())frames.append(safe);
    }
    if(!frames.isEmpty())output.insert("frames",frames);
    QVariantList causes;
    for(const auto &item:input.value("causes").toList().mid(0,3)) {
        const auto cause=item.toMap();QVariantMap safe;
        const auto causeType=fixedName(cause.value("type"));
        if(!causeType.isEmpty())safe.insert("type",causeType);
        if(number(cause.value("errno")))safe.insert("errno",cause.value("errno"));
        if(!safe.isEmpty())causes.append(safe);
    }
    if(!causes.isEmpty())output.insert("causes",causes);
    return output;
}
QVariantMap record(const QVariant &value, const QString &forcedSource={}) {
    const auto input=value.toMap();
    const auto time=timestamp(input.value("time"));
    const auto level=input.value("level").toString();
    const auto component=fixedName(input.value("component"),64),event=fixedName(input.value("event"),64);
    const auto source=forcedSource.isEmpty()?input.value("source").toString():forcedSource;
    if(!time.isValid() || component.isEmpty() || event.isEmpty()
        || !QStringList({"debug","info","warning","error","critical"}).contains(level)
        || !QStringList({"desktop","backend","fan","wifi","firmware"}).contains(source)) return {};
    QVariantMap result{{"time",time.toString(Qt::ISODateWithMs)},{"level",level},{"component",component},{"event",event},{"source",source}};
    for(const auto &key:{"pid","suppressed","request_id","http","error_class","duration_ms","line"})
        if(number(input.value(key)))result.insert(key,input.value(key));
    for(const auto &key:{"session","operation_id","method","file"}) {
        const auto name=fixedName(input.value(key));if(!name.isEmpty())result.insert(key,name);
    }
    if(input.value("truncated").metaType().id()==QMetaType::Bool)result.insert("truncated",input.value("truncated"));
    static const QStringList fields={"operation","stage","outcome","reason","scope","attribute","store","phase","component","job_id","worker_id","errno","returncode","duration_ms","pending","enabled","restored","source","count","retryable","supported","version","code","status","bytes","repeated","kernel","python","architecture"};
    QVariantMap context;
    const auto sourceContext=input.value("context").toMap();
    for(const auto &key:fields) {
        if(!sourceContext.contains(key))continue;
        const auto item=sourceContext.value(key);
        if(item.metaType().id()==QMetaType::QString)context.insert(key,safeText(item.toString()));
        else if(number(item) || item.metaType().id()==QMetaType::Bool)context.insert(key,item);
    }
    if(!context.isEmpty())result.insert("context",context);
    const auto error=errorMetadata(input.value("error"));if(!error.isEmpty())result.insert("error",error);
    if(QJsonDocument::fromVariant(result).toJson(QJsonDocument::Compact).size()>4096)return {};
    return result;
}
struct LocalResult { QVariantList records;QStringList warnings;bool readable=false; };
LocalResult readLocal(const QString &directory) {
    LocalResult result;
    if(directory.isEmpty()) { result.warnings.append("desktop_logs_unavailable");return result; }
#ifdef Q_OS_UNIX
    const auto encoded=QFile::encodeName(directory);
    const int dir=::open(encoded.constData(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);
    struct stat directoryInfo {};
    if(dir<0 || ::fstat(dir,&directoryInfo) || directoryInfo.st_uid!=::getuid()) {
        if(dir>=0)::close(dir);
        result.warnings.append("desktop_logs_unavailable");return result;
    }
#else
    const QFileInfo directoryInfo(directory);
    if(!directoryInfo.isDir() || directoryInfo.isSymLink()) { result.warnings.append("desktop_logs_unavailable");return result; }
#endif
    result.readable=true;
    for(const auto &leaf:{QString("desktop-events.jsonl.2"),QString("desktop-events.jsonl.1"),QString("desktop-events.jsonl")}) {
        QFile file;
#ifdef Q_OS_UNIX
        const int fd=::openat(dir,QFile::encodeName(leaf).constData(),O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);
        if(fd<0) { if(errno!=ENOENT)result.warnings.append("desktop_log_unreadable");continue; }
        struct stat info {};
        if(::fstat(fd,&info) || !S_ISREG(info.st_mode) || info.st_uid!=::getuid() || info.st_nlink!=1) {
            ::close(fd);result.warnings.append("desktop_log_unsafe");continue;
        }
        if(!file.open(fd,QIODevice::ReadOnly,QFileDevice::AutoCloseHandle)) {
            ::close(fd);result.warnings.append("desktop_log_unreadable");continue;
        }
#else
        const auto path=QDir(directory).filePath(leaf);const QFileInfo info(path);
        if(!info.exists())continue;
        if(!info.isFile() || info.isSymLink()) { result.warnings.append("desktop_log_unsafe");continue; }
        file.setFileName(path);
        if(!file.open(QIODevice::ReadOnly)) { result.warnings.append("desktop_log_unreadable");continue; }
#endif
        const auto size=file.size();bool clipped=size>FileBytes;
        if(clipped && !file.seek(size-FileBytes)) { result.warnings.append("desktop_log_unreadable");continue; }
        const auto bytes=file.read(FileBytes);
        if(file.error()!=QFileDevice::NoError) { result.warnings.append("desktop_log_unreadable");continue; }
        if(clipped)result.warnings.append("desktop_log_truncated");
        const auto lines=bytes.split('\n');
        for(int index=clipped?1:0;index<lines.size();++index) {
            const auto line=lines.at(index).trimmed();if(line.isEmpty())continue;
            if(line.size()>4096) { result.warnings.append("desktop_log_malformed");continue; }
            QJsonParseError parse;const auto json=QJsonDocument::fromJson(line,&parse);
            if(parse.error!=QJsonParseError::NoError || !json.isObject()) { result.warnings.append("desktop_log_malformed");continue; }
            const auto safe=record(json.toVariant(),"desktop");
            if(safe.isEmpty())result.warnings.append("desktop_log_malformed");else result.records.append(safe);
        }
    }
#ifdef Q_OS_UNIX
    ::close(dir);
#endif
    result.warnings.removeDuplicates();
    // A failed file read is not evidence that its earlier entries disappeared.
    // Keep the last complete native snapshot until all retained files are readable.
    if(result.warnings.contains("desktop_log_unreadable") || result.warnings.contains("desktop_log_unsafe"))result.readable=false;
    return result;
}
struct ExportResult { QString path,error; };
ExportResult writeExport(const QString &directory,const QByteArray &bytes,const QString &stamp) {
    ExportResult result;
    if(directory.isEmpty()) { result.error="Desktop folder is unavailable.";return result; }
#ifdef Q_OS_UNIX
    const int dir=::open(QFile::encodeName(directory).constData(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);
    struct stat info {};
    if(dir<0 || ::fstat(dir,&info) || info.st_uid!=::getuid()) {
        if(dir>=0)::close(dir);result.error="Desktop folder is unavailable.";return result;
    }
#else
    const QFileInfo info(directory);
    if(!info.isDir() || info.isSymLink()) { result.error="Desktop folder is unavailable.";return result; }
#endif
    for(int suffix=0;suffix<100;++suffix) {
        const auto leaf="LegionGoCompanion-logs-"+stamp+(suffix?QString("-%1").arg(suffix):QString())+".jsonl";
        const auto path=QDir(directory).filePath(leaf);QFile file;
#ifdef Q_OS_UNIX
        const int fd=::openat(dir,QFile::encodeName(leaf).constData(),O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC,0600);
        if(fd<0) { if(errno==EEXIST)continue;result.error="Cannot create a log file on the Desktop.";break; }
        struct stat created {};
        if(::fstat(fd,&created) || !S_ISREG(created.st_mode) || created.st_uid!=::getuid() || created.st_nlink!=1 || ::fchmod(fd,0600)
            || !file.open(fd,QIODevice::WriteOnly,QFileDevice::AutoCloseHandle)) {
            ::close(fd);::unlinkat(dir,QFile::encodeName(leaf).constData(),0);result.error="Cannot create a private log file.";break;
        }
#else
        file.setFileName(path);
        if(!file.open(QIODevice::WriteOnly|QIODevice::NewOnly)) {
            if(QFileInfo::exists(path) || QFileInfo(path).isSymLink())continue;
            result.error="Cannot create a log file on the Desktop.";break;
        }
        if(!file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner)) {
            file.close();QFile::remove(path);result.error="Cannot create a private log file.";break;
        }
#endif
        qint64 written=0;
        while(written<bytes.size()) {
            const auto count=file.write(bytes.constData()+written,bytes.size()-written);
            if(count<=0)break;
            written+=count;
        }
        const bool success=written==bytes.size() && file.flush();file.close();
        if(!success) {
#ifdef Q_OS_UNIX
            ::unlinkat(dir,QFile::encodeName(leaf).constData(),0);
#else
            QFile::remove(path);
#endif
            result.error="Log export failed. The incomplete file was removed.";
        } else result.path=path;
        break;
    }
#ifdef Q_OS_UNIX
    ::close(dir);
#endif
    if(result.path.isEmpty() && result.error.isEmpty())result.error="Cannot choose an unused log filename.";
    return result;
}
QString filterValue(const QString &value) { return value=="all"?QString():value; }
QString nativeLogDirectory() {
    const auto state=QStandardPaths::writableLocation(QStandardPaths::GenericStateLocation);
    return state.isEmpty()?QString():QDir(state).filePath("legiongo2companion");
}
}

LogViewer::LogViewer(RpcClient *rpc,QObject *parent)
    :LogViewer(rpc,nativeLogDirectory(),
        QStandardPaths::writableLocation(QStandardPaths::DesktopLocation),parent) {}
LogViewer::LogViewer(RpcClient *rpc,QString localLogDir,QString desktopDir,QObject *parent)
    :QObject(parent),rpc_(rpc),localLogDir_(std::move(localLogDir)),desktopDir_(std::move(desktopDir)) {
    if(rpc_)connect(rpc_,&RpcClient::finished,this,[this](int id,const QString &method,const QVariant &value,const QString &error) {
        if(!loading_ || backendDone_ || id!=request_ || method!="desktop_get_logs")return;
        backendDone_=true;backendFailed_=!error.isEmpty();
        const auto result=value.toMap();
        if(!backendFailed_ && result.value("events").metaType().id()!=QMetaType::QVariantList)backendFailed_=true;
        if(!backendFailed_) {
            QVariantList rows;
            bool malformed=false;
            for(const auto &item:result.value("events").toList().mid(0,RecordLimit)) {
                const auto safe=record(item);if(!safe.isEmpty())rows.append(safe);else malformed=true;
            }
            backend_=rows;backendWarnings_.clear();availableSources_.clear();
            for(const auto &warning:result.value("warnings").toStringList()) {
                const auto safe=fixedName(warning);if(!safe.isEmpty())backendWarnings_.append(safe);
            }
            if(malformed)backendWarnings_.append("backend_log_malformed");
            for(const auto &source:result.value("available_sources").toStringList())
                if(QStringList({"backend","fan","wifi","firmware"}).contains(source))availableSources_.append(source);
            truncated_=result.value("truncated").toBool();
        }
        finishRefresh();
    });
}
void LogViewer::refresh() {
    if(loading_)return;
    loading_=true;localDone_=false;backendDone_=false;backendFailed_=false;
    error_.clear();emit loadingChanged();emit changed();
    const auto result=std::make_shared<LocalResult>();const auto directory=localLogDir_;
    auto *thread=QThread::create([result,directory] { *result=readLocal(directory); });
    connect(thread,&QThread::finished,this,[this,result] {
        localDone_=true;localWarnings_=result->warnings;
        if(result->readable)local_=result->records;
        finishRefresh();
    });
    connect(thread,&QThread::finished,thread,&QObject::deleteLater);thread->start();
    if(rpc_)request_=rpc_->call("desktop_get_logs",{},false);
    else { backendDone_=true;backendFailed_=true; }
}
void LogViewer::finishRefresh() {
    if(!localDone_ || !backendDone_)return;
    snapshot_=backend_;snapshot_.append(local_);
    std::sort(snapshot_.begin(),snapshot_.end(),[](const QVariant &left,const QVariant &right) {
        const auto a=timestamp(left.toMap().value("time")),b=timestamp(right.toMap().value("time"));
        if(a!=b)return a>b;
        return QJsonDocument::fromVariant(left).toJson(QJsonDocument::Compact)<QJsonDocument::fromVariant(right).toJson(QJsonDocument::Compact);
    });
    const bool clipped=snapshot_.size()>RecordLimit;
    if(clipped)snapshot_=snapshot_.mid(0,RecordLimit);
    warnings_=backendWarnings_;warnings_.append(localWarnings_);
    if(truncated_ || clipped)warnings_.append("record_limit_reached");
    if(backendFailed_)warnings_.append("backend_logs_unavailable");
    warnings_.removeDuplicates();stale_=backendFailed_ || localWarnings_.contains("desktop_logs_unavailable")
        || localWarnings_.contains("desktop_log_unreadable") || localWarnings_.contains("desktop_log_unsafe");
    error_=backendFailed_?QString("Backend logs are unavailable. Previously loaded entries are retained."):QString();
    loading_=false;applyFilters();emit loadingChanged();emit changed();
}
void LogViewer::applyFilters() {
    filtered_.clear();const auto since=QDateTime::currentDateTimeUtc().addSecs(-qint64(ageMinutes_)*60);
    for(const auto &item:snapshot_) {
        const auto row=item.toMap();const auto severity=row.value("level").toString();
        if(!level_.isEmpty()) {
            if(level_=="warning" && !QStringList({"warning","error","critical"}).contains(severity))continue;
            if(level_=="error" && !QStringList({"error","critical"}).contains(severity))continue;
            if(level_!="warning" && level_!="error" && severity!=level_)continue;
        }
        if(!source_.isEmpty() && row.value("source")!=source_)continue;
        if(!component_.isEmpty() && row.value("component")!=component_)continue;
        if(ageMinutes_>0 && timestamp(row.value("time"))<since)continue;
        if(!search_.isEmpty() && !QString::fromUtf8(QJsonDocument::fromVariant(row).toJson(QJsonDocument::Compact)).contains(search_,Qt::CaseInsensitive))continue;
        filtered_.append(row);
    }
    status_=QString("%1 of %2 entries").arg(filtered_.size()).arg(snapshot_.size());
    if(stale_)status_+=" · refresh incomplete";
}
void LogViewer::filterChanged() { applyFilters();emit filtersChanged();emit changed(); }
void LogViewer::setSearch(const QString &value) { const auto next=value.left(256);if(search_!=next){search_=next;filterChanged();} }
void LogViewer::setLevel(const QString &value) { const auto next=filterValue(value);if(level_!=next){level_=next;filterChanged();} }
void LogViewer::setSource(const QString &value) { const auto next=filterValue(value);if(source_!=next){source_=next;filterChanged();} }
void LogViewer::setComponent(const QString &value) { const auto next=filterValue(value);if(component_!=next){component_=next;filterChanged();} }
void LogViewer::setAgeMinutes(int value) { const auto next=qBound(0,value,525600);if(ageMinutes_!=next){ageMinutes_=next;filterChanged();} }
QStringList LogViewer::sourceOptions() const {
    QStringList result=availableSources_;for(const auto &item:snapshot_)result.append(item.toMap().value("source").toString());
    if(!source_.isEmpty())result.append(source_);
    result.removeDuplicates();result.sort();return result;
}
QStringList LogViewer::componentOptions() const {
    QStringList result;for(const auto &item:snapshot_)result.append(item.toMap().value("component").toString());
    if(!component_.isEmpty())result.append(component_);
    result.removeDuplicates();result.sort();return result;
}
void LogViewer::exportFiltered() {
    if(exporting_)return;
    if(filtered_.isEmpty()) { error_="There are no matching entries to export.";emit changed();return; }
    QByteArray bytes;
    for(const auto &row:filtered_)bytes.append(QJsonDocument::fromVariant(row).toJson(QJsonDocument::Compact)+"\n");
    exporting_=true;exportedPath_.clear();error_.clear();emit exportingChanged();emit changed();
    const auto result=std::make_shared<ExportResult>();const auto directory=desktopDir_;
    const auto stamp=QDateTime::currentDateTimeUtc().toString("yyyyMMdd-HHmmss-zzz'Z'");
    auto *thread=QThread::create([result,directory,bytes,stamp] { *result=writeExport(directory,bytes,stamp); });
    connect(thread,&QThread::finished,this,[this,result] {
        exporting_=false;exportedPath_=result->path;error_=result->error;
        emit exportingChanged();emit changed();
    });
    connect(thread,&QThread::finished,thread,&QObject::deleteLater);thread->start();
}
QString LogViewer::writeSnapshot(const QString &directory,const QByteArray &bytes,const QString &stamp,QString &error) {
    const auto result=writeExport(directory,bytes,stamp);error=result.error;return result.path;
}
