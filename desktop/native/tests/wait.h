// SPDX-License-Identifier: BSD-3-Clause
#pragma once
#include <QtTest>
// QTRY_* expand to QtTest internals that changed signature in Qt 6.11. CI also runs
// these suites against a newer Qt than they were built with, so wait via public API.
#define WAIT_VERIFY_T(condition, ms) QVERIFY2(QTest::qWaitFor([&] { return bool(condition); }, QDeadlineTimer(ms)), #condition)
#define WAIT_VERIFY(condition) WAIT_VERIFY_T(condition, 5000)
#define WAIT_COMPARE_T(actual, expected, ms) \
    do { (void)QTest::qWaitFor([&] { return (actual) == (expected); }, QDeadlineTimer(ms)); QCOMPARE(actual, expected); } while (false)
#define WAIT_COMPARE(actual, expected) WAIT_COMPARE_T(actual, expected, 5000)
