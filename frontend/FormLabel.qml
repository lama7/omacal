import QtQuick
import qs.Commons
import qs.Ui

// Reusable label for the add/edit form rows: fixed 56-space width, dimmed
// foreground, body-small font, left+vertically centered in its parent row.
// The panel root is passed in for its contentForeground/contentFontFamily;
// guard against panel being null at bind time (it's set after creation).
Text {
    id: root
    property var formPanel: null
    width: Style.space(56)
    color: formPanel ? Qt.darker(formPanel.contentForeground, 1.5) : Color.foreground
    font.family: formPanel ? formPanel.contentFontFamily : Style.font.family
    font.pixelSize: Style.font.bodySmall
    verticalAlignment: Text.AlignVCenter
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
}
