# Матрица «требование → реализация → доказательство»

Исходный документ: «Кейс DevOps (MTC ENGINEER HACK).docx». Номера критериев сохранены. Таблица показывает реализованные артефакты и необходимые runtime-проверки, **не подменяет их фактическим PASS**.

| Критерий | Реализация в репозитории | Проверка эксперта |
|---|---|---|
| 1 / 30: Kubernetes, приоритет kubeadm | `scripts/bootstrap-ubuntu.sh`, `versions.env`, Cilium values | Ubuntu host + API /version, `make acceptance` |
| 1: простое приложение | Nginx stable/canary, Deployments/Services, JSON access log | точное `Hello World!`, headers, 200 |
| 1: Gateway API | Class/Gateway/HTTPRoute, EnvoyProxy, controller Helm | NodePort curl, current conditions, TLS/hostname/path |
| 2 / 25: инструкция и автоматизация | Makefile, shell bootstrap, renderer, deploy waits/dry-run | команды README на чужой выделенной VM |
| 2: повторный запуск | стабильные шаблоны, config checksum, сохранение secrets/PVC | `make acceptance`: два deploy, UID и certificate invariants |
| 2: зависимости | фиксированные версии, image-lock resolver, chart vendor/checksums | `make lock`, `make vendor`, commit файлов, inventory imageID |
| 3 / 20: Prometheus | standalone Deployment/PVC, 6 scrape jobs, alerts | `/targets` up, не пустой query, свежий scrape, счётчик запросов |
| 3: Fluentd | CRI tail DaemonSet, persistent positions/buffer | настоящий control request + Loki query по proof_id |
| 3: access/error logs | stdout JSON + stderr text, Loki filesystem TSDB | 200 access и 404 /missing с найденным error ID |
| 4 / 15: структура/код | детерминированные manifests, stdlib, unit tests | `make static`, review config → generated snapshots |
| 4: безопасность | non-root, read-only, drop capabilities, probes/PDB, Cilium policies | static tests + restricted cross-namespace Job |
| 4: секреты | локальная генерация, Kubernetes Secret, .gitignore | нет private data в manifests/Git; повторный deploy не ротирует |
| 5 / 10: документация | README, ADR, runbook, demo, паспорт ≤4 стр. | быстрый запуск и проверка каждым указанным способом |
| 5: CI/CD | `ci.yml`: static + kind; `ubuntu-kubeadm.yml`: отдельная Ubuntu VM | реальный green workflow, artifacts и точный commit; приёмка выполняется |
| 5: Gateway extras | host/path/header, два backend, local-CA TLS, traffic splitting | `make verify`, `make demo`, `make canary` |
| 5: observability extras | Grafana, blackbox, node metrics, alerts, finite retention | UI, query, reports; external notifications не заявлены |
| 5: дополнительная особенность | fail-closed evidence, bad-canary rollback, Pod recovery drill | реальный `evidence/demo/report.json` |
| 5: буфер логов при отказе | сохранение positions/buffer, короткая недоступность Loki и guarded restore | `make log-delivery`: уникальные ID, задержки, пропуски и наблюдаемые дубли |
| Обязательная ОС | целевой Ubuntu 24.04 amd64 + kubeadm 1.35.9 | **подтверждение ожидается**, флаг `ubuntu_24_04_kubeadm_confirmed` |
| Формат сдачи | `scripts/package.py`, docs/SUBMISSION.md | два файла, публичный main, настоящий URL, размеры/страницы |

Оценка баллов не прогнозируется: наличие строки в матрице не гарантирует балл, если соответствующий эксперимент не проходит.
