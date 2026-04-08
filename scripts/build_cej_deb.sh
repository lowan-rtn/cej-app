#!/bin/sh
set -eu

APP_NAME="cej-dashboard"
VERSION="${1:-0.1.0}"
ARCH="all"
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(dirname "$ROOT_DIR")"
BUILD_ROOT="${PROJECT_DIR}/build/${APP_NAME}_${VERSION}"
PKG_ROOT="${BUILD_ROOT}/pkg"
APP_ROOT="${PKG_ROOT}/opt/${APP_NAME}"
BIN_ROOT="${PKG_ROOT}/usr/bin"
DESKTOP_ROOT="${PKG_ROOT}/usr/share/applications"
ICON_ROOT="${PKG_ROOT}/usr/share/icons/hicolor/scalable/apps"
DOC_ROOT="${PKG_ROOT}/usr/share/doc/${APP_NAME}"
VENV_SRC="${HOME}/.local/share/cej-dashboard/venv"
DEB_PATH="${PROJECT_DIR}/build/${APP_NAME}_${VERSION}_${ARCH}.deb"

rm -rf "${BUILD_ROOT}"
mkdir -p "${APP_ROOT}/app" "${APP_ROOT}/venv" "${BIN_ROOT}" "${DESKTOP_ROOT}" "${ICON_ROOT}" "${DOC_ROOT}" "${PKG_ROOT}/DEBIAN"

cp "${ROOT_DIR}/cej_desktop.py" "${APP_ROOT}/app/"
cp "${ROOT_DIR}/cej_web_ui.py" "${APP_ROOT}/app/"
cp "${ROOT_DIR}/login_cej.py" "${APP_ROOT}/app/"
cp "${ROOT_DIR}/list_cej_actions.py" "${APP_ROOT}/app/"
cp "${ROOT_DIR}/create_cej_action.py" "${APP_ROOT}/app/"
cp "${ROOT_DIR}/update_cej_action.py" "${APP_ROOT}/app/"
cp "${ROOT_DIR}/delete_cej_action.py" "${APP_ROOT}/app/"
cp "${ROOT_DIR}/cej-dashboard.svg" "${ICON_ROOT}/${APP_NAME}.svg"
cp -a "${VENV_SRC}/." "${APP_ROOT}/venv/"

cat > "${BIN_ROOT}/${APP_NAME}" <<'EOF'
#!/bin/sh
set -eu
APP_ROOT="/opt/cej-dashboard"
VENV_SITE="${APP_ROOT}/venv/lib/python3.13/site-packages"
if [ -d "${VENV_SITE}" ]; then
  if [ "${PYTHONPATH:-}" != "" ]; then
    export PYTHONPATH="${VENV_SITE}:${PYTHONPATH}"
  else
    export PYTHONPATH="${VENV_SITE}"
  fi
fi
exec /usr/bin/python3 "${APP_ROOT}/app/cej_desktop.py" "$@"
EOF
chmod 0755 "${BIN_ROOT}/${APP_NAME}"

cat > "${DESKTOP_ROOT}/${APP_NAME}.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Version=1.0
Name=Tableau de bord CEJ
Comment=Ouvre le tableau de bord CEJ en application desktop
Exec=/usr/bin/cej-dashboard
Icon=cej-dashboard
Terminal=false
Categories=Office;Utility;
StartupNotify=true
EOF

cat > "${DOC_ROOT}/copyright" <<'EOF'
Packaged locally for personal CEJ dashboard use.
EOF

cat > "${PKG_ROOT}/DEBIAN/control" <<EOF
Package: ${APP_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Maintainer: lreutin-rab-cc
Depends: python3, python3-pyqt5, python3-pyqt5.qtwebengine
Description: Tableau de bord CEJ desktop
 Local CEJ dashboard desktop app with weekly agenda, actions management and local settings.
EOF

dpkg-deb --build "${PKG_ROOT}" "${DEB_PATH}"
printf '%s\n' "${DEB_PATH}"
