# Runbook: от симптома к проверяемой причине

Сначала `export PATH="$PWD/.tools:$PATH" KUBECONFIG="$PWD/.state/kubeconfig"`. Не публиковать Secrets, admin.conf и содержимое `.state/`. При сбое сохраните runtime report; не начинайте с `kubeadm reset`, удаления PVC или отключения NetworkPolicy.

<a id="gateway"></a>

## Приложение недоступно

```bash
kubectl get nodes -o wide
kubectl -n signal get pods,svc,endpointslices
kubectl -n signal get gateway signal -o yaml
kubectl -n signal get httproute web-main -o yaml
kubectl -n envoy-gateway-system get pods,svc
kubectl -n envoy-gateway-system logs deployment/envoy-gateway --tail=100
```

Сначала различить transport и routing: неверный Host даёт ожидаемый 404, недоступный NodePort — сеть/доступ, 503 — нет backend или намеренная faulty canary. `Accepted=True` с прежним observedGeneration не доказывает принятие новой конфигурации. `make deploy` возвращает stable 100%, `make verify` проверяет фактический ответ.

При TLS не использовать `-k`: `--resolve signal.local:30443:IP` задаёт правильный SNI и адрес. Убедиться, что CA соответствует кластеру. Обновление сертификатов — отдельная операция: сохранить старые данные, выпустить новый leaf с тем же trust anchor либо осознанно заменить CA и trust store; автоматического renew в проекте нет.

## Pod Pending / PVC Pending

```bash
kubectl -n signal-observe get pvc
kubectl get pv
kubectl -n signal-system describe job signal-storage-init
kubectl -n signal-observe describe pod <имя-pod>
```

Local PV имеют привязку к конкретному hostname. Не переносить их на другой узел редактированием affinity: там нет данных. Проверить свободную память/диск и существование `/var/lib/signal-storage/*`, владельца каталога и полный Job. Retain после удаления claim оставляет PV Released: для повторного использования требуется ручная проверка данных и привязки; автоматическая очистка claimRef запрещена по умолчанию, чтобы не подключить чужие данные.

<a id="metrics"></a>

## Target down

```bash
kubectl -n signal-observe logs deployment/prometheus --tail=100
kubectl -n signal logs deployment/web-stable -c nginx-exporter --tail=100
kubectl -n kube-system get pods -l k8s-app=cilium
```

В Prometheus `/targets` посмотреть lastError, адрес и job. Nginx exporter идёт к loopback stub_status своего Pod; Cilium разрешает scrape 9113 только от Prometheus. Envoy proxy использует `/stats/prometheus`, controller — другой endpoint; proxy discovery специально ограничен owning-gateway label. Не добавлять allow-all, пока не установлена точная причина.

## Лог не находится

```bash
kubectl -n signal-logging get pods
kubectl -n signal-logging logs daemonset/fluentd --tail=120
kubectl -n signal-observe logs deployment/loki --tail=120
kubectl -n signal logs deployment/web-stable -c nginx --tail=20
```

Разделить три точки: запись существует в приложении; Fluentd tail видит CRI файл и может писать buffer; Loki принимает batch и query смотрит нужный диапазон времени. Проверить часовую синхронизацию узла, метки namespace/app/stream, отсутствие старого proof_id. Проверять `make verify`, а не только stdout агента. Не удалять position/buffer как первое средство: это создаёт replay/дубли или потерю данных.

Если Fluentd падает до запуска monitor API, посмотреть также `kubectl -n signal-logging logs daemonset/fluentd --previous`. Ruby отвергает world-writable `/tmp` без sticky bit; в проекте задан `TMPDIR=/buffers`, у root-owned каталога не должно быть записи для остальных пользователей. `make validate-containers` проверяет реальный запуск supervisor с read-only rootfs и drop ALL, а не только dry-run конфигурации.

<a id="storage"></a>

## Диск заполнен

```bash
df -h /
sudo du -sh /var/lib/signal-storage/* /var/lib/signal-fluentd
```

Приостановить тестовую нагрузку, установить причину: TSDB, WAL, buffer, system logs или images. Retention не является мгновенным освобождением места; размер local PV не ограничивает файловую систему. Не удалять живые WAL/chunks произвольно. Для демо безопаснее восстановить выделенную VM из осознанного snapshot; для эксплуатации нужен отдельный протестированный backup/restore процесс, которого этот репозиторий не заявляет.

## Ошибка image pull / скачивания

Проверить исходный hostname, DNS, прокси, лимиты Docker Hub и точный тег. `make lock` должен завершиться полностью; частичный lock не сохраняется. Не менять на latest, не отключать TLS verification и не использовать неизвестный mirror. Любая замена версии требует повторного render/static/deploy/acceptance и обновления README/паспорта.

## Общий lock: другая операция уже работает

Bootstrap, kind/deploy и Python acceptance, canary/demo, log-delivery согласуют изменения через `.state/workflow.lock`. `Another SIGNAL operation is running.` означает, что второй процесс не получил kernel lock и не должен менять стенд. Дождаться завершения владельца или прервать именно его, затем изучить его отчёт и recovery marker. Не удалять lock-файл и не подставлять `SIGNAL_OPERATION_LOCK_FD`: inherited FD проверяется по настоящему файлу/дескриптору, а не по наличию переменной.

```bash
# Только проверка: команда сразу завершится ошибкой, если lock ещё занят.
flock -n .state/workflow.lock -c true
```

Наличие `.state/workflow.lock` после завершения процесса нормально. Блокировка освобождается ядром после закрытия последнего владеющего дескриптора; дочерний deploy может ещё работать, даже если родитель завершён. Port-forward не получает этот lock. Другая рабочая копия и прямой `kubectl` не координируются этим механизмом. Standalone `make verify` пока не удерживает общий lock; запускать его после остановки изменяющих состояние операций, либо использовать в составе acceptance.

## После canary / прерванного сценария

```bash
make deploy
make verify
```

Успешный `make canary` специально оставляет 100% canary; verifier baseline ожидает stable. При нормальном `make demo` canary и веса возвращаются. Полученные до завершения операции `SIGINT` / `SIGTERM` вызывают попытку cleanup под тем же lock и проверку фактического baseline; прерванный сценарий завершается неуспешно даже после успешного восстановления. Для canary обработка позднего сигнала при удалении marker или записи отчёта также заканчивается восстановлением и FAIL, пока port-forward доступны. Повторные SIGINT/SIGTERM во время canary cleanup записываются, не прерывая его. После обработки marker и финальной записи отчёта canary-операция считается завершённой: сигнал при последующем закрытии forward только печатает сообщение и не отменяет уже успешные 100%. Восстановление после обоих сигналов подтверждено Ubuntu и kind для `f99de235d92b`; конкретные probes находятся в evidence/published/*/operations/report.json.

При `SIGKILL`, выключении VM или недоступности API не считать rollback выполненным. Сначала дождаться завершения старых команд, проверить свободный lock, восстановить доступ к API и прочитать recovery marker. Canary marker описывает требуемый baseline; Loki marker содержит исходные UID и число реплик. Если Loki Deployment заменён, UID отличается или обнаружены чужие изменения, прекратить автоматическое восстановление и разобрать изменения вручную. Не удалять marker до проверки восстановленного объекта и пользовательского HTTP-ответа.

Для canary штатный возврат к baseline — `make deploy`, затем `make verify`: это восстанавливает здоровую canary и маршрут stable 100%. Оставшийся `.state/canary-restore.json` запрещает новые canary/demo, но не мешает deploy. Deploy сам marker не удаляет. После остановки остальных операций прочитать его и выполнить:

```bash
cat .state/canary-restore.json
make deploy && make verify && rm -- .state/canary-restore.json
```

При ошибке deploy/verify последняя команда не выполняется: сохранить marker и диагностировать отчёт. Если оба marker остались одновременно, сначала восстановить Loki, затем canary: полная проверка требует доступного Loki и здорового приложения.

Для прерванного Loki-опыта прочитать `.state/log-delivery-restore.json` и сравнить UID `Deployment/loki`. Следующая ручная процедура использует существующий guarded restore: под общим lock она проверяет UID и число реплик, разрешает scale только из 0 в сохранённое значение с `--current-replicas=0`, ждёт rollout и повторно проверяет UID/replicas. Если реплики уже равны baseline, scale не нужен; другое значение отвергается. Marker удаляется только после полной проверки HTTP, метрик и поиска логов:

```bash
cat .state/log-delivery-restore.json
python3 - <<'PY'
import json, sys
sys.path.insert(0, 'scripts')
from operations import operation_lock
from log_delivery import restore_loki
from runtime import ROOT, require, run
with operation_lock(ROOT):
    marker = ROOT / '.state/log-delivery-restore.json'
    baseline = json.loads(marker.read_text(encoding='utf-8'))
    require(isinstance(baseline.get('uid'), str) and baseline['uid'], 'Invalid saved Loki UID')
    require(type(baseline.get('replicas')) is int and baseline['replicas'] >= 1,
            'Invalid saved Loki replica count')
    restore_loki(baseline)
    print(run([sys.executable, 'scripts/verify.py'], timeout=900))
    marker.unlink()
PY
```

Это явное действие оператора, а не фоновая recovery CLI. Не заменять UID в marker и не подставлять желаемое число реплик, чтобы обойти отказ. Если Loki восстановлен, но полная проверка не проходит из-за оставшейся faulty canary, сохранить Loki marker, восстановить canary по предыдущей процедуре и повторить проверку. Удалять каждый marker только после успешной проверки.

### Проверка самого протокола прерываний

После baseline `make deploy && make verify` на выделенном Linux-стенде выполнить `make operation-check`. Он отвергает настоящие конкурирующие deploy/canary/log-delivery/acceptance, проверяет неизменность конфигурации, посылает SIGTERM PID после faulty injection и SIGINT группе процессов здорового продвижения. Успех требует FAIL прерванного дочернего отчёта, записанный сигнал, проверенный stable/canary HTTP baseline, удалённый canary marker и свободный lock. Результат находится в `evidence/operations/report.json`; для сдачи он должен совпадать с чистым HEAD финального acceptance. Отказ API и прерывания Loki этот протокол не проверяет; предыдущие зелёные отчёты не являются его результатом.

Canary и deploy меняют полный `web-main` через Server-Side Apply с одинаковым manager `signal`. Императивный patch создаёт отдельное владение Update и может конфликтовать с последующим deploy. При конфликте с чужим manager сначала выяснить источник изменения; автоматическое `--force-conflicts` не применяется.

## Деактивация стенда

kind: `kind delete cluster --name signal` удаляет этот Docker-кластер и его локальные данные. Для kubeadm-профиля предпочтительно удалить выделенную VM после сохранения нужных отчётов. Автоматической команды wipe/reset нет, чтобы случайный запуск не уничтожил кластер или storage.
