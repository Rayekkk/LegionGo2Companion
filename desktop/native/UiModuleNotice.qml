// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
RowLayout {
    id:notice
    required property var status
    signal manageRequested()
    UiTheme { id:theme }
    visible:!status.ready
    spacing:12
    Label { textFormat:Text.PlainText; text:notice.status.message;color:notice.status.state==="disabled"?theme.muted:theme.warning;wrapMode:Text.WordWrap;Layout.fillWidth:true;font.pixelSize:12 }
    UiButton { text:"Modules";visible:notice.status.fresh && notice.status.state!=="blocked" && notice.status.state!=="maintenance";onClicked:notice.manageRequested();implicitHeight:32;font.pixelSize:12 }
}
