cmake_minimum_required(VERSION 3.24)
project(LegionGoCompanionDesktop VERSION 0.1 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Qt6 6.9 REQUIRED COMPONENTS Core Gui Widgets Network Qml Quick QuickControls2)
qt_standard_project_setup(REQUIRES 6.9)
qt_add_executable(legiongo2companion main.cpp rpcclient.cpp rpcclient.h apphost.cpp apphost.h logviewer.cpp logviewer.h)
# The app runs on the distribution's Qt (SteamOS), which updates over time:
# no build-tree RUNPATH, and QML bytecode only (the engine recompiles from the
# embedded sources when the runtime's bytecode differs) instead of C++ that
# links QQmlPrivate AOT helpers.
set_target_properties(legiongo2companion PROPERTIES WIN32_EXECUTABLE TRUE BUILD_WITH_INSTALL_RPATH TRUE INSTALL_RPATH ""
    QT_QMLCACHEGEN_ARGUMENTS --only-bytecode)
qt_add_qml_module(legiongo2companion URI Companion VERSION 1.0
    QML_FILES Main.qml Backend.qml FanPage.qml CurveGraph.qml ModulePage.qml OverviewPage.qml
        UiCard.qml UiField.qml UiTheme.qml UiButton.qml UiSwitch.qml UiComboBox.qml UiBadge.qml UiIcon.qml UiMetric.qml UiReadout.qml UiSlider.qml UiOptions.qml ModuleStatus.qml UiModuleNotice.qml UiConfirm.qml AppSettings.qml LogsPage.qml fan.mjs wifi.mjs tdp.mjs battery.mjs rgb.mjs vibration.mjs remap.mjs controller.mjs firmware.mjs display.mjs modules.mjs
    RESOURCES rpc-methods.json)
target_link_libraries(legiongo2companion PRIVATE Qt6::Gui Qt6::Widgets Qt6::Network Qt6::Qml Qt6::Quick Qt6::QuickControls2)
if(MSVC)
    target_compile_options(legiongo2companion PRIVATE /W4 /WX /permissive-)
    # MSVC 19.51 reports unreachable Qt metatype templates in generated QML
    # cache code. Suppress only that diagnostic on Qt-generated sources.
    foreach(page Main Backend FanPage CurveGraph ModulePage OverviewPage UiCard UiField UiTheme UiButton UiSwitch UiComboBox UiBadge UiIcon UiMetric UiReadout UiSlider UiOptions ModuleStatus UiModuleNotice UiConfirm AppSettings LogsPage)
        set_source_files_properties("${CMAKE_CURRENT_BINARY_DIR}/.rcc/qmlcache/legiongo2companion_${page}_qml.cpp"
            PROPERTIES COMPILE_OPTIONS /wd4702)
    endforeach()
else()
    target_compile_options(legiongo2companion PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()
option(BUILD_TESTING "Build repository-only native tests" OFF)
include(CTest)
if(BUILD_TESTING)
    find_package(Qt6 REQUIRED COMPONENTS Test QuickTest)
    qt_add_executable(native_transport_tests tests/transport.cpp tests/wait.h rpcclient.cpp rpcclient.h)
    target_link_libraries(native_transport_tests PRIVATE Qt6::Core Qt6::Network Qt6::Test)
    qt_add_resources(native_transport_tests contract PREFIX "/qt/qml/Companion" FILES rpc-methods.json)
    add_test(NAME transport COMMAND native_transport_tests)
    qt_add_executable(native_logviewer_tests tests/logviewer.cpp tests/wait.h logviewer.cpp logviewer.h rpcclient.cpp rpcclient.h)
    target_link_libraries(native_logviewer_tests PRIVATE Qt6::Core Qt6::Gui Qt6::Network Qt6::Test)
    qt_add_resources(native_logviewer_tests contract PREFIX "/qt/qml/Companion" FILES rpc-methods.json)
    add_test(NAME logviewer COMMAND native_logviewer_tests)
    set_tests_properties(logviewer PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen")
    qt_add_executable(native_host_tests tests/host.cpp apphost.cpp apphost.h)
    target_link_libraries(native_host_tests PRIVATE Qt6::Widgets Qt6::Quick Qt6::Test)
    add_test(NAME native_host COMMAND native_host_tests)
    set_tests_properties(native_host PROPERTIES ENVIRONMENT "QT_QPA_PLATFORM=offscreen")
    qt_add_executable(native_ui_tests tests/quick.cpp rpcclient.cpp rpcclient.h)
    target_link_libraries(native_ui_tests PRIVATE Qt6::QuickTest Qt6::QuickControls2 Qt6::Network)
    target_compile_definitions(native_ui_tests PRIVATE QUICK_TEST_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/qml")
    add_test(NAME native_ui COMMAND native_ui_tests)
    set_tests_properties(native_ui PROPERTIES ENVIRONMENT "QT_QUICK_BACKEND=software;QT_QPA_PLATFORM=offscreen;QT_QUICK_CONTROLS_STYLE=Basic")
endif()
install(TARGETS legiongo2companion RUNTIME DESTINATION .)
