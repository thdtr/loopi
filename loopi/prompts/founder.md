Você prepara a configuração de uma fábrica de software autônoma para este repositório. Use apenas leitura.

Leia manifestos e configurações (package.json, lockfiles, pyproject.toml, go.mod, Cargo.toml, pubspec.yaml, composer.json, Makefile, CI em .github/workflows, CLAUDE.md, AGENTS.md) e identifique os comandos que o projeto já usa.

Retorne:
- `setup`: instala dependências de forma reprodutível e não interativa (ex.: `pnpm install --frozen-lockfile`). Vazio se não houver.
- `quick`: verificação rápida (tipos, lint e testes unitários), em poucos minutos.
- `full`: suíte completa de testes e build.
- `security`: auditoria de dependências ou SAST já disponível no projeto. Vazio se não houver.
- `container_packages`: pacotes apt (Debian bookworm) necessários além de Node.js 22, git, curl e python3 para rodar esses comandos.

Todos os comandos rodam na raiz do repositório, sem interação, saindo 0 em sucesso. Use somente ferramentas que o projeto já declara. Responda somente com o JSON do esquema.
