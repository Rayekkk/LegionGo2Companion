// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
// The one confirmation dialog: same titles, texts and OK labels as the Decky panel.
Dialog {
    id:dialog
    property string text:""
    property string okText:"OK"
    property bool danger:false
    UiTheme { id:theme }
    modal:true; standardButtons:Dialog.Ok|Dialog.Cancel
    anchors.centerIn:Overlay.overlay
    width:520 // the window is never narrower than 980 px
    font.pixelSize:theme.font
    onAboutToShow: {
        const ok=standardButton(Dialog.Ok);
        if(ok) { ok.text=okText; ok.palette.buttonText=danger?theme.danger:theme.text; }
    }
    contentItem:Label { textFormat:Text.PlainText; text:dialog.text; wrapMode:Text.WordWrap; color:theme.text; font.pixelSize:theme.font }
}
