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

## После canary / аварийно оборванной демонстрации

```bash
make deploy
make verify
```

Успешный `make canary` специально оставляет 100% canary; verifier baseline ожидает stable. При нормальном `make demo` canary и веса возвращаются. При SIGKILL/выключении VM Python finally не выполнится; это не промышленная гарантия rollback.

## Деактивация стенда

kind: `kind delete cluster --name signal` удаляет этот Docker-кластер и его локальные данные. Для kubeadm-профиля предпочтительно удалить выделенную VM после сохранения нужных отчётов. Автоматической команды wipe/reset нет, чтобы случайный запуск не уничтожил кластер или storage.
