// SPDX-License-Identifier: BSD-3-Clause
#pragma once
#include <QObject>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QElapsedTimer>
#include <QQueue>
#include <QSet>
#include <QVariant>

// Presentation client only. This process never opens EC, HID or sysfs devices.
class RpcClient final : public QObject {
    Q_OBJECT
    Q_PROPERTY(int pending READ pending NOTIFY pendingChanged)
public:
    explicit RpcClient(QString sessionPath, QObject *parent = nullptr);
    int pending() const;
    Q_INVOKABLE int call(const QString &method, const QVariantList &args = {}, bool mutation = false);
    Q_INVOKABLE void cancelReads();
    Q_INVOKABLE double monotonicSeconds() const;
    static bool readSession(const QString &path, QString &origin, QByteArray &token, QString &error);
signals:
    void finished(int id, QString method, QVariant result, QString error);
    void pendingChanged();
private:
    struct Request { int id; QString method; QVariantList args; bool mutation; };
    QString sessionPath_;
    QNetworkAccessManager network_;
    QQueue<Request> queue_;
    QSet<QString> methods_;
    QElapsedTimer clock_;
    QNetworkReply *reply_ = nullptr;
    Request active_ {};
    QByteArray response_;
    bool oversize_ = false;
    int nextId_ = 1;
    void startNext();
};
