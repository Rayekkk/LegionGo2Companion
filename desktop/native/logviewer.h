// SPDX-License-Identifier: BSD-3-Clause
#pragma once
#include <QObject>
#include <QStringList>
#include <QVariant>

class RpcClient;

// Presentation-only diagnostics. Neither reading nor exporting controls the engine.
class LogViewer final : public QObject {
    Q_OBJECT
    Q_PROPERTY(QVariantList records READ records NOTIFY changed)
    Q_PROPERTY(bool loading READ loading NOTIFY loadingChanged)
    Q_PROPERTY(bool exporting READ exporting NOTIFY exportingChanged)
    Q_PROPERTY(bool stale READ stale NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(QString status READ status NOTIFY changed)
    Q_PROPERTY(QString exportedPath READ exportedPath NOTIFY changed)
    Q_PROPERTY(QStringList warnings READ warnings NOTIFY changed)
    Q_PROPERTY(QStringList sourceOptions READ sourceOptions NOTIFY changed)
    Q_PROPERTY(QStringList componentOptions READ componentOptions NOTIFY changed)
    Q_PROPERTY(int filteredCount READ filteredCount NOTIFY changed)
    Q_PROPERTY(int totalCount READ totalCount NOTIFY changed)
    Q_PROPERTY(QString search READ search WRITE setSearch NOTIFY filtersChanged)
    Q_PROPERTY(QString level READ level WRITE setLevel NOTIFY filtersChanged)
    Q_PROPERTY(QString source READ source WRITE setSource NOTIFY filtersChanged)
    Q_PROPERTY(QString component READ component WRITE setComponent NOTIFY filtersChanged)
    Q_PROPERTY(int ageMinutes READ ageMinutes WRITE setAgeMinutes NOTIFY filtersChanged)
public:
    explicit LogViewer(RpcClient *rpc, QObject *parent = nullptr);
    LogViewer(RpcClient *rpc, QString localLogDir, QString desktopDir, QObject *parent = nullptr);
    QVariantList records() const { return filtered_; }
    bool loading() const { return loading_; }
    bool exporting() const { return exporting_; }
    bool stale() const { return stale_; }
    QString error() const { return error_; }
    QString status() const { return status_; }
    QString exportedPath() const { return exportedPath_; }
    QStringList warnings() const { return warnings_; }
    QStringList sourceOptions() const;
    QStringList componentOptions() const;
    int filteredCount() const { return int(filtered_.size()); }
    int totalCount() const { return int(snapshot_.size()); }
    QString search() const { return search_; }
    QString level() const { return level_; }
    QString source() const { return source_; }
    QString component() const { return component_; }
    int ageMinutes() const { return ageMinutes_; }
    void setSearch(const QString &value);
    void setLevel(const QString &value);
    void setSource(const QString &value);
    void setComponent(const QString &value);
    void setAgeMinutes(int value);
    Q_INVOKABLE void refresh();
    Q_INVOKABLE void exportFiltered();
signals:
    void changed();
    void filtersChanged();
    void loadingChanged();
    void exportingChanged();
private:
    friend class LogViewerTests;
    static QString writeSnapshot(const QString &directory, const QByteArray &bytes, const QString &stamp, QString &error);
    RpcClient *rpc_;
    QString localLogDir_, desktopDir_;
    QVariantList backend_, local_, snapshot_, filtered_;
    QStringList backendWarnings_, localWarnings_, warnings_, availableSources_;
    QString search_, level_, source_, component_, error_, status_, exportedPath_;
    int ageMinutes_ = 0, request_ = 0;
    bool loading_ = false, exporting_ = false, stale_ = false;
    bool localDone_ = false, backendDone_ = false, backendFailed_ = false, truncated_ = false;
    void finishRefresh();
    void applyFilters();
    void filterChanged();
};
