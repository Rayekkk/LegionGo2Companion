// SPDX-License-Identifier: BSD-3-Clause
#include "rpcclient.h"
#include "diagnostics.h"
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QNetworkProxy>
#include <QRegularExpression>
#include <QTimer>
#ifdef Q_OS_UNIX
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

RpcClient::RpcClient(QString path, QObject *parent) : QObject(parent), sessionPath_(std::move(path)) {
    network_.setProxy(QNetworkProxy::NoProxy);
    clock_.start();
    QFile contract(":/qt/qml/Companion/rpc-methods.json");
    if (contract.open(QIODevice::ReadOnly)) {
        for (const auto value : QJsonDocument::fromJson(contract.readAll()).array())
            methods_.insert(value.toString());
    }
    methods_.insert("desktop_context");
    methods_.insert("desktop_get_logs");
}
int RpcClient::pending() const { return queue_.size() + (reply_ ? 1 : 0); }
double RpcClient::monotonicSeconds() const { return clock_.nsecsElapsed() / 1e9; }

bool RpcClient::readSession(const QString &path, QString &origin, QByteArray &token, QString &error) {
    QFile file;
#ifdef Q_OS_UNIX
    const int fd = ::open(QFile::encodeName(path).constData(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    struct stat info {};
    if (fd < 0) { error = "Start Decky and Companion, then reconnect."; return false; }
    if (::fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != ::getuid() ||
        (info.st_mode & 0077) || info.st_size > 4096) {
        ::close(fd); error = "Unsafe Desktop session file."; return false;
    }
    if (!file.open(fd, QIODevice::ReadOnly, QFileDevice::AutoCloseHandle)) {
        ::close(fd); error = "Cannot read Desktop session."; return false;
    }
#else
    const QFileInfo info(path);
    if (!info.isFile() || info.isSymLink() || info.size() > 4096) {
        error = "Missing or unsafe Desktop session file."; return false;
    }
    file.setFileName(path);
    if (!file.open(QIODevice::ReadOnly)) { error = "Cannot read Desktop session."; return false; }
#endif
    const auto bytes = file.read(4097);
    QJsonParseError parse;
    const auto document = QJsonDocument::fromJson(bytes, &parse);
    if (bytes.size() > 4096 || parse.error != QJsonParseError::NoError || !document.isObject()) {
        error = "Invalid Desktop session."; return false;
    }
    origin = document.object().value("origin").toString();
    token = document.object().value("token").toString().toLatin1();
    static const QRegularExpression address("^http://127\\.0\\.0\\.1:([0-9]{1,5})$");
    static const QRegularExpression capability("^[A-Za-z0-9_-]{43}$");
    const auto match = address.match(origin);
    if (!match.hasMatch() || match.captured(1).toInt() < 1 || match.captured(1).toInt() > 65535 ||
        !capability.match(QString::fromLatin1(token)).hasMatch()) {
        error = "Invalid Desktop session."; return false;
    }
    return true;
}

int RpcClient::call(const QString &method, const QVariantList &args, bool mutation) {
    const int id = nextId_++;
    if (!methods_.contains(method) || args.size() > 12 || pending() >= 24) {
        CompanionDiagnostics::event("rpc_admission_rejected",methods_.contains(method)?method:QString(),id);
        QTimer::singleShot(0, this, [this,id,method] { emit finished(id, method, {}, "Request unavailable or queue full."); });
        return id;
    }
    queue_.enqueue({id,method,args,mutation});
    emit pendingChanged();
    QTimer::singleShot(0, this, &RpcClient::startNext);
    return id;
}
void RpcClient::cancelReads() {
    // Never cancel admitted writes or diagnostics cleanup on navigation/exit.
    QQueue<Request> retained;
    while (!queue_.isEmpty()) {
        auto request = queue_.dequeue();
        if (request.mutation) retained.enqueue(request);
        else emit finished(request.id,request.method,{},"Obsolete read.");
    }
    queue_ = retained;
    emit pendingChanged();
}
void RpcClient::startNext() {
    if (reply_ || queue_.isEmpty()) return;
    active_ = queue_.dequeue();
    QString origin, error;
    QByteArray token;
    if (!readSession(sessionPath_, origin, token, error)) {
        CompanionDiagnostics::change("backend_session","unavailable");
        emit finished(active_.id,active_.method,{},error);
        emit pendingChanged();
        QTimer::singleShot(0,this,&RpcClient::startNext);
        return;
    }
    CompanionDiagnostics::change("backend_session","available");
    QNetworkRequest request(QUrl(origin + "/rpc"));
    request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,QNetworkRequest::ManualRedirectPolicy);
    request.setRawHeader("Origin",origin.toUtf8());
    request.setRawHeader("Authorization","Bearer " + token);
    request.setHeader(QNetworkRequest::ContentTypeHeader,"application/json");
    const int timeout=active_.mutation?155000:(active_.method=="get_version" || active_.method=="desktop_context"?4000:20000);
    request.setTransferTimeout(timeout);
    const QJsonObject object {{"method",active_.method},{"args",QJsonArray::fromVariantList(active_.args)}};
    response_.clear(); oversize_ = false;
    reply_ = network_.post(request,QJsonDocument(object).toJson(QJsonDocument::Compact));
    reply_->setProperty("diagnosticStarted",clock_.elapsed());
    auto *deadline = new QTimer(reply_);
    deadline->setSingleShot(true);
    connect(deadline,&QTimer::timeout,reply_,&QNetworkReply::abort);
    deadline->start(timeout); // Wall deadline as well as the transfer inactivity timeout.
    reply_->setReadBufferSize(512 * 1024 + 1);
    connect(reply_,&QIODevice::readyRead,this,[this] {
        response_.append(reply_->readAll());
        if (response_.size() > 512 * 1024) { oversize_ = true; reply_->abort(); }
    });
    connect(reply_,&QNetworkReply::finished,this,[this] {
        if(reply_->isOpen())response_.append(reply_->readAll());
        const int status = reply_->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        QVariant result; QString message;
        QJsonParseError parse;
        const auto document = QJsonDocument::fromJson(response_,&parse);
        if (oversize_ || response_.size() > 512 * 1024) message = "Backend response exceeded the limit.";
        else if (status >= 300 && status < 400) message = "Backend redirect rejected.";
        else if (status == 200 && reply_->error() != QNetworkReply::NoError)
            message = "Connection lost. Refresh before retrying a change.";
        else if (parse.error != QJsonParseError::NoError || !document.isObject())
            message = "Backend unavailable. Refresh before retrying a change.";
        else if (document.object().contains("error")) {
            message = document.object().value("error").toString().trimmed().left(1000);
            if (message.isEmpty()) message = "The backend could not complete this request. Refresh its state.";
        }
        else if (status != 200 || !document.object().contains("result")) message = "Change not confirmed. Refresh its state.";
        else {
            result = document.object().value("result").toVariant();
            const auto map = result.toMap();
            if (map.contains("success") && !map.value("success").toBool()) {
                for (const auto &key : {"error", "reason", "stderr"}) {
                    message = map.value(key).toString().trimmed().left(1000);
                    if (!message.isEmpty()) break;
                }
                if (message.isEmpty()) message = "Change not confirmed. Refresh its state.";
            }
        }
        CompanionDiagnostics::rpc(active_.method,active_.id,!message.isEmpty(),active_.mutation,
            status,int(reply_->error()),clock_.elapsed()-reply_->property("diagnosticStarted").toLongLong());
        reply_->deleteLater(); reply_ = nullptr;
        const auto completed = active_;
        emit pendingChanged();
        emit finished(completed.id,completed.method,result,message);
        QTimer::singleShot(0,this,&RpcClient::startNext);
    });
}
