// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
QtObject {
    readonly property color background: "#171c24"
    readonly property color sidebar: "#131820"
    readonly property color card: "#202731"
    readonly property color input: "#2b3441"
    readonly property color inputHover: "#354151"
    readonly property color border: "#35404e"
    readonly property color divider: "#303a47"
    readonly property color selected: "#23344f"
    readonly property color hover: "#252f3c"
    readonly property color text: "#e4ebf2"
    readonly property color muted: "#98a7b8"
    readonly property color faint: "#8a98aa"
    readonly property color accent: "#2865ff"
    readonly property color accentHover: "#4178ff"
    readonly property color accentText: "#86b0ff"
    readonly property color success: "#5fd19b"
    readonly property color warning: "#e6ba6b"
    readonly property color danger: "#ec6370"
    // Touch first: 48 px targets, 16 px body text, notes never below 13 px.
    readonly property int target: 48
    readonly property int font: 16
    readonly property int fontSmall: 14
    readonly property int fontTiny: 13
    // Layout: pages stop growing at contentWidth (left aligned under the title); a page with one
    // card keeps it at formWidth; two cards per row from twoColumns; one gap between all cards.
    readonly property int contentWidth: 1240
    readonly property int formWidth: 760
    readonly property int twoColumns: 960
    readonly property int gap: 18
}
