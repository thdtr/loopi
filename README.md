# Loopi

Você escreve o que quer. O Loopi constrói, testa e integra sozinho, usando o Claude Code.

## Instalar

```sh
curl -fsSL https://raw.githubusercontent.com/thdtr/loopi/main/install.sh | sh
```

Precisa ter: `git`, `python3` (3.10+), [Claude Code](https://code.claude.com) e `podman` ou `docker`.

## Usar

**1. Entre no seu projeto** (precisa ser um repositório git com pelo menos um commit)

```sh
cd meu-projeto
loopi init
```

**2. Escreva o que você quer** num arquivo, por exemplo `docs/specs/001-login.md`. Diga o resultado esperado e o porquê.

**3. Escreva como provar que funciona.** Crie scripts em `.loopi/scenarios/`. Cada script sai com `0` se funcionou. O agente nunca vê essa pasta.

```sh
#!/bin/sh
# .loopi/scenarios/001-login/login-ok.sh
curl -fs localhost:3000/login -d 'user=ana&pass=123' | grep -q bem-vinda
```

```sh
chmod +x .loopi/scenarios/001-login/login-ok.sh
git add -A && git commit -m "spec 001"
```

**4. Gere as tarefas**

```sh
loopi plan docs/specs/001-login.md
```

**5. Ligue a fábrica**

```sh
claude setup-token                       # uma vez; copie o token
export CLAUDE_CODE_OAUTH_TOKEN=cole-aqui
loopi run
```

**6. Veja como está**

```sh
loopi status
```

Pronto. O que passou em tudo já está no seu ramo principal. O que precisa de você aparece no `loopi status`.

## Comandos

| Comando | O que faz |
|---|---|
| `loopi init` | Cria a fábrica no projeto |
| `loopi plan <spec>` | Quebra a spec em tarefas |
| `loopi run` | Executa as tarefas até a fila acabar |
| `loopi run --continuous` | Fica rodando sem parar |
| `loopi status` | Mostra o que foi feito e o que precisa de você |
| `loopi retry <id>` | Devolve uma tarefa para a fila |
| `loopi discard <id>` | Joga uma tarefa fora |
| `loopi improve` | Inspeciona o app e sugere tarefas |
| `loopi stop` / `loopi resume` | Pausa e retoma |

## Ajustes

Tudo fica em `.loopi/loopi.json`. Os mais usados:

| Campo | Para quê |
|---|---|
| `commands.quick` | Teste rápido do projeto |
| `commands.full` | Teste completo |
| `commands.deploy` / `smoke` / `rollback` | Publicar, conferir e desfazer |
| `commands.incidents` | Comando que lista erros de produção em JSON |
| `sensitive_paths` | Pastas que sempre viram pull request para você revisar |
| `budget.daily_usd` | Limite de gasto por dia |

## Atualizar e remover

Atualizar: rode o mesmo comando de instalação.
Remover: `rm -rf ~/.local/share/loopi ~/.local/bin/loopi`

## Licença

MIT
