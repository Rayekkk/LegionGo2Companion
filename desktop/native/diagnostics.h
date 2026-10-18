// SPDX-License-Identifier: BSD-3-Clause
#pragma once
#include <QDateTime>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QHash>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMutex>
#include <QRegularExpression>
#include <QScopeGuard>
#include <QUuid>
#include <QCoreApplication>
#ifdef Q_OS_UNIX
#include <cerrno>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

// Fixed event codes and numeric transport context only. No raw Qt messages,
// session paths, tokens, arguments, bodies or backend error text enter this API.
namespace CompanionDiagnostics {
inline constexpr qint64 MaxBytes=1024*1024;
struct State {
    QMutex mutex;
    QString directory, session=QUuid::createUuid().toString(QUuid::Id128).left(12);
    QElapsedTimer clock;
    QHash<QString,qint64> repeats;
    QHash<QString,QString> states;
    bool boundary=false;
#ifdef Q_OS_UNIX
    int directoryFd=-1;
    ~State() { if(directoryFd>=0)::close(directoryFd); }
#endif
};
inline State &state() { static State value;return value; }
inline QString label(const QString &value) {
    static const QRegularExpression syntax("^[A-Za-z0-9_.-]{1,64}$");
    return syntax.match(value).hasMatch()?value:QString("unknown");
}
#ifdef Q_OS_UNIX
inline bool safeFile(int directory,const QByteArray &name) {
    struct stat info {};
    if(::fstatat(directory,name.constData(),&info,AT_SYMLINK_NOFOLLOW))return errno==ENOENT;
    return S_ISREG(info.st_mode) && info.st_uid==::getuid() && info.st_nlink==1 && !(info.st_mode&0077);
}
#else
inline bool safeFile(const QString &path) {
    const QFileInfo info(path);
    return !info.isSymLink() && (!info.exists() || info.isFile());
}
#endif
inline bool configure(const QString &directory) noexcept {
    try {
        auto &s=state();QMutexLocker guard(&s.mutex);
#ifdef Q_OS_UNIX
        if(s.directoryFd>=0)::close(s.directoryFd);
        s.directoryFd=-1;
#endif
        s.directory.clear();s.repeats.clear();s.states.clear();s.boundary=false;s.clock.start();
        if(directory.isEmpty())return false;
        if(!QDir().mkpath(QFileInfo(directory).absolutePath()))return false;
#ifdef Q_OS_UNIX
        const auto encoded=QFile::encodeName(directory);
        if(::mkdir(encoded.constData(),0700) && errno!=EEXIST)return false;
        const int fd=::open(encoded.constData(),O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);
        if(fd<0)return false;
        struct stat info {};
        if(::fstat(fd,&info) || !S_ISDIR(info.st_mode) || info.st_uid!=::getuid() || ::fchmod(fd,0700)) {
            ::close(fd);return false;
        }
        s.directoryFd=fd;
#else
        const QFileInfo before(directory);
        if(before.isSymLink() || (before.exists() && !before.isDir()))return false;
        if(!QDir().mkpath(directory))return false;
#endif
        s.directory=directory;
        return true;
    } catch(...) { return false; }
}

inline bool append(State &s,const QByteArray &record) {
    const QByteArray leaf("desktop-events.jsonl");
#ifdef Q_OS_UNIX
    for(const auto &name:{leaf,leaf+".1",leaf+".2"})
        if(!safeFile(s.directoryFd,name))return false;
    int fd=::openat(s.directoryFd,leaf.constData(),O_WRONLY|O_CREAT|O_APPEND|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC,0600);
    if(fd<0)return false;
    struct stat info {};
    if(::fstat(fd,&info) || !S_ISREG(info.st_mode) || info.st_uid!=::getuid() || info.st_nlink!=1 || ::fchmod(fd,0600)) {
        ::close(fd);return false;
    }
    if(info.st_size+record.size()>MaxBytes) {
        ::close(fd);
        const auto older=leaf+".2",previous=leaf+".1";
        if(::unlinkat(s.directoryFd,older.constData(),0) && errno!=ENOENT)return false;
        if(::renameat(s.directoryFd,previous.constData(),s.directoryFd,older.constData()) && errno!=ENOENT)return false;
        if(::renameat(s.directoryFd,leaf.constData(),s.directoryFd,previous.constData()))return false;
        fd=::openat(s.directoryFd,leaf.constData(),O_WRONLY|O_CREAT|O_EXCL|O_APPEND|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC,0600);
        if(fd<0)return false;
    }
    const auto written=::write(fd,record.constData(),size_t(record.size()));
    ::close(fd);return written==record.size();
#else
    const auto path=QDir(s.directory).filePath(QString::fromLatin1(leaf));
    for(const auto &name:{path,path+".1",path+".2"})if(!safeFile(name))return false;
    if(QFileInfo(path).size()+record.size()>MaxBytes) {
        if(QFileInfo::exists(path+".2") && !QFile::remove(path+".2"))return false;
        if(QFileInfo::exists(path+".1") && !QFile::rename(path+".1",path+".2"))return false;
        if(QFileInfo::exists(path) && !QFile::rename(path,path+".1"))return false;
    }
    QFile file(path);
    if(!file.open(QIODevice::WriteOnly|QIODevice::Append))return false;
    if(!file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner))return false;
    return file.write(record)==record.size();
#endif
}

inline bool event(const QString &code,const QString &method={},int request=0,
                  int http=0,int errorClass=-1,qint64 duration=-1,bool coalesce=true,
                  const QString &file={},int line=0) noexcept {
    try {
        auto &s=state();if(!s.mutex.tryLock())return false;
        const auto unlock=qScopeGuard([&s]{s.mutex.unlock();});
        if(s.directory.isEmpty())return false;
        const QString name=label(code),operation=method.isEmpty()?QString():label(method);
        const QString key=name+":"+operation+":"+QString::number(http)+":"+QString::number(errorClass)
            +":"+label(QFileInfo(file).fileName())+":"+QString::number(line);
        const auto now=s.clock.elapsed();
        if(coalesce && s.repeats.contains(key) && now-s.repeats.value(key)<60000)return true;
        const bool warning=name.contains("failed") || name.contains("rejected") || name.contains("unavailable")
            || name.contains("warning") || name.contains("deadline") || name.contains("unknown") || name.contains("missing");
        QJsonObject object{{"time",QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)},
            {"component","desktop"},{"level",warning?"warning":"info"},
            {"event",name},{"pid",QCoreApplication::applicationPid()},{"session",s.session}};
        if(!operation.isEmpty())object.insert("method",operation);
        if(request>0)object.insert("request_id",request);
        if(http)object.insert("http",http);
        if(errorClass>=0)object.insert("error_class",errorClass);
        if(duration>=0)object.insert("duration_ms",duration);
        if(!file.isEmpty()) { object.insert("file",label(QFileInfo(file).fileName()));object.insert("line",line); }
        const auto record=QJsonDocument(object).toJson(QJsonDocument::Compact)+"\n";
        if(record.size()>4096)return false;
        s.boundary=!append(s,(s.boundary?QByteArray("\n"):QByteArray())+record);
        if(s.boundary)return false;
        if(s.repeats.size()>=128)s.repeats.clear();
        s.repeats.insert(key,now);
        return true;
    } catch(...) { return false; }
}
inline void change(const QString &key,const QString &value) noexcept {
    try {
        auto &s=state();
        {
            QMutexLocker guard(&s.mutex);
            if(s.directory.isEmpty() || s.states.value(key)==value)return;
        }
        if(event(label(key)+"_"+label(value),{},0,0,0,-1,false)) {
            QMutexLocker guard(&s.mutex);
            if(s.states.size()>=128)s.states.clear();
            s.states.insert(key,value);
        }
    } catch(...) { }
}
inline void rpc(const QString &method,int request,bool failed,bool mutation,int http,int network,qint64 duration) noexcept {
    try {
        auto &s=state();const auto key="rpc_"+label(method);bool wasFailed=false;
        {
            QMutexLocker guard(&s.mutex);
            if(s.directory.isEmpty())return;
            wasFailed=s.states.value(key)=="failed";
        }
        if(failed) {
            if(event("rpc_failed",method,request,http,network,duration,wasFailed)) {
                QMutexLocker guard(&s.mutex);
                if(s.states.size()>=128)s.states.clear();
                s.states.insert(key,"failed");
            }
        }
        else if(wasFailed) {
            if(event("rpc_recovered",method,request,http,network,duration,false)) {
                QMutexLocker guard(&s.mutex);s.states.remove(key);
            }
        }
        // The panel lease fires on every page change; only its failures are worth a record.
        else if(mutation && method!="set_panel_active")event("rpc_completed",method,request,http,network,duration,false);
    } catch(...) { }
}
} // namespace CompanionDiagnostics
