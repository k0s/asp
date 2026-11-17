# The sandbox

A throwaway place to watch asp work: sample pictures, the thumbnail rule from
[snakewatchers](https://github.com/k0s/snakewatchers), a journal, and the
dashboard, reconciling every five seconds.

```sh
ansible-playbook sandbox/playbook.yml
```

Then open <http://127.0.0.1:8765/> and drop a picture into `~/asp-sandbox/site`.
Within a few seconds it has a `thumbs/thumb_<name>` beside it, and the event and
the job are on the page.

| | |
|---|---|
| `~/asp-sandbox/site` | the one root; put files here |
| `~/asp-sandbox/state/asp.db` | the journal; delete it to forget history |
| `~/asp-sandbox/asp.json` | the config the service runs |
| `~/asp-sandbox/.venv` | asp, installed editable from this checkout |
| `asp-sandbox.service` | a systemd user unit: `systemctl --user status asp-sandbox` |

The playbook needs `uv` and `ansible`, no root, and changes nothing on a second
run. asp is installed from the checkout the playbook sits in, so after editing
asp, `systemctl --user restart asp-sandbox` runs the new code.

Variables, with `-e name=value`:

| variable | default | |
|---|---|---|
| `sandbox_dir` | `~/asp-sandbox` | where everything lives |
| `sandbox_port` | `8765` | the dashboard's port, on 127.0.0.1 |
| `sandbox_interval` | `5` | seconds between passes |
| `sandbox_service` | `true` | `false` installs without a service |
| `snakewatchers_source` | the GitHub repo | a checkout path works too |
| `sandbox_reinstall` | `false` | `true` reinstalls asp, after its dependencies change |
| `sandbox_state` | `present` | `absent` stops and removes the service |

`sandbox_state=absent` leaves `~/asp-sandbox` in place; delete it by hand.

The CLI works against the same sandbox:

```sh
cd ~/asp-sandbox
.venv/bin/asp status
.venv/bin/asp events -n 20
.venv/bin/asp jobs --state parked
```
