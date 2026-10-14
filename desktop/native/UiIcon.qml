// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Shapes
Item {
    id:icon
    property string name:"overview"
    property color color:"#98a7b8"
    implicitWidth:22; implicitHeight:22
    readonly property var paths:({
        overview:"M3 3H10V10H3Z M14 3H21V10H14Z M3 14H10V21H3Z M14 14H21V21H14Z",
        tdp:"M13 2L5 13H11L10 22L19 10H13Z",
        fan:"M12 10C7 2 17 1 17 6C17 9 14 11 12 12 M14 12C23 10 24 20 19 20C16 20 13 16 12 12 M11 14C10 23 1 19 3 15C4 12 9 11 12 12 M14 12A2 2 0 1 1 10 12A2 2 0 1 1 14 12",
        vibration:"M2 10V14 M5 7V17 M8 4V20 M12 8V16 M16 4V20 M19 7V17 M22 10V14",
        rgb:"M12 3A9 9 0 1 1 12 21A9 9 0 1 1 12 3 M12 7A5 5 0 1 1 12 17A5 5 0 1 1 12 7",
        remap:"M3 5H21V19H3Z M7 9H9 M11 9H13 M15 9H17 M7 13H9 M11 13H13 M15 13H17 M8 16H16",
        controller:"M7 7H17C20 7 22 18 19 18L15 15H9L5 18C2 18 4 7 7 7Z M8 10V14 M6 12H10 M16 10H16.1 M18 13H18.1",
        battery:"M2 6H19V18H2Z M21 10V14 M5 9H13V15H5Z",
        display:"M3 4H21V17H3Z M8 21H16 M12 17V21",
        wifi:"M2 8C8 2 16 2 22 8 M5 11C9 7 15 7 19 11 M8 14C11 11 13 11 16 14 M12 17H12.1",
        modules:"M12 2L22 8L12 14L2 8Z M2 12L12 18L22 12 M2 16L12 22L22 16",
        settings:"M5 4H19V20H5Z M8 8H16 M8 12H16 M8 16H16",
        logs:"M5 3H16L20 7V21H5Z M16 3V7H20 M8 11H17 M8 15H17 M8 18H14",
        about:"M12 3A9 9 0 1 1 12 21A9 9 0 1 1 12 3 M12 10V17 M12 7H12.1",
        arrow:"M4 12H20 M14 6L20 12L14 18",
        refresh:"M20 7V3 M20 7H16 M20 7A9 9 0 1 0 21 15"
    })
    Item {
        width:24; height:24; scale:Math.min(icon.width,icon.height)/24
        anchors.centerIn:parent
        Shape { anchors.fill:parent; preferredRendererType:Shape.CurveRenderer
            ShapePath { strokeColor:icon.color; strokeWidth:1.65; fillColor:"transparent"; capStyle:ShapePath.RoundCap; joinStyle:ShapePath.RoundJoin
                PathSvg { path:icon.paths[icon.name] || icon.paths.overview }
            }
        }
    }
}
