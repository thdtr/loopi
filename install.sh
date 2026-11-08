#!/bin/sh
set -eu

REPO="thdtr/loopi"
VERSION="${LOOPI_VERSION:-latest}"
INSTALL_DIR="${LOOPI_DIR:-$HOME/.local/share/loopi}"
BIN_DIR="${LOOPI_BIN:-$HOME/.local/bin}"

fail() {
  printf 'loopi: %s\n' "$1" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || fail "curl não encontrado"
command -v tar >/dev/null 2>&1 || fail "tar não encontrado"
command -v git >/dev/null 2>&1 || fail "git não encontrado"
command -v python3 >/dev/null 2>&1 || fail "python3 não encontrado"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail "python3 3.10 ou mais novo é necessário"

if [ "$VERSION" = "latest" ]; then
  URL="https://github.com/$REPO/releases/latest/download/loopi.tar.gz"
else
  URL="https://github.com/$REPO/releases/download/v${VERSION#v}/loopi.tar.gz"
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

curl -fsSL "$URL" -o "$TMP/loopi.tar.gz" || fail "não foi possível baixar $URL"
mkdir -p "$TMP/loopi"
tar -xzf "$TMP/loopi.tar.gz" -C "$TMP/loopi"
[ -f "$TMP/loopi/bin/loopi" ] || fail "pacote inválido"

rm -rf "$INSTALL_DIR"
mkdir -p "$(dirname "$INSTALL_DIR")" "$BIN_DIR"
mv "$TMP/loopi" "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/bin/loopi"
ln -sf "$INSTALL_DIR/bin/loopi" "$BIN_DIR/loopi"

printf 'loopi %s instalado em %s\n' "$(cat "$INSTALL_DIR/VERSION")" "$BIN_DIR/loopi"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf "adicione ao seu PATH: export PATH=\"%s:\$PATH\"\n" "$BIN_DIR" ;;
esac

command -v claude >/dev/null 2>&1 || printf 'falta o Claude Code: curl -fsSL https://claude.ai/install.sh | bash\n'
if ! command -v podman >/dev/null 2>&1 && ! command -v docker >/dev/null 2>&1; then
  printf 'recomendado: instale podman ou docker para isolar o agente\n'
fi
