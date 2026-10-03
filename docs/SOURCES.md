# Основания выбора и первичные источники

Дата внешней проверки при подготовке: 3 октября 2026. Требования и баллы взяты из предоставленного пользователем документа «Кейс DevOps (MTC ENGINEER HACK).docx». Его текст не заменяется общими рекомендациями. Решения SIGNAL — авторский проект поверх требований, а не предписанный организаторами стек.

Официальные источники для версий/совместимости/конфигурации:

- Kubernetes releases: https://kubernetes.io/releases/ — patch 1.35.9 и поддерживаемые ветки.
- kubeadm installation: https://kubernetes.io/docs/setup/production-environment/tools/kubeadm/install-kubeadm/ — стандартная установка.
- Envoy Gateway matrix: https://gateway.envoyproxy.io/news/releases/matrix/ — 1.9 / K8s 1.33–1.36 / Gateway API 1.6.1; предупреждение о несовместимой независимой версии Envoy.
- EG quickstart: https://gateway.envoyproxy.io/docs/tasks/quickstart/ — chart v1.9.2 и проверка через Gateway Service.
- EG customization: https://gateway.envoyproxy.io/docs/tasks/operations/customize-envoyproxy/ — собственный EnvoyProxy и patch ресурсов.
- EG API reference: https://gateway.envoyproxy.io/docs/api/extension_types/ — KubernetesServiceSpec name/type/patch.
- EG metrics: https://gateway.envoyproxy.io/docs/tasks/observability/proxy-metric/ — proxy metrics 19001 /stats/prometheus.
- EG chart source: https://raw.githubusercontent.com/envoyproxy/gateway/v1.9.2/charts/gateway-helm/values.tmpl.yaml — deployment.envoyGateway/resources и controller defaults.
- Cilium compatibility: https://docs.cilium.io/en/stable/network/kubernetes/compatibility/ — ветка 1.20.2 и Kubernetes 1.35.
- Cilium kubeadm: https://docs.cilium.io/en/stable/installation/k8s-install-kubeadm/ — установка CNI для kubeadm.
- kind release: https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0 — node image v1.35.8 и digest; это не v1.35.9.
- Helm release: https://github.com/helm/helm/releases/tag/v3.20.2 — бинарный checksum.
- Nginx stable changelog: https://nginx.org/en/CHANGES-1.30 — 1.30.5.
- Nginx exporter: https://github.com/nginx/nginx-prometheus-exporter/releases/tag/v1.5.0
- Prometheus 3.13.4 (security/bug fixes): https://github.com/prometheus/prometheus/releases/tag/v3.13.4
- GitHub Ubuntu runner images: https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md - отдельная Ubuntu VM для основного acceptance.
- Prometheus downloads: https://prometheus.io/download/ — выбранные Prometheus/exporters/Alertmanager версии.
- Loki releases: https://github.com/grafana/loki/releases — 3.7.8.
- Loki Fluentd client: https://grafana.com/docs/loki/latest/send-data/fluentd/ — plugin, labels, JSON, buffer, автоматически добавляемый push path.
- Fluentd image tags: https://hub.docker.com/r/grafana/fluent-plugin-loki/tags — тег 3.7.8.
- Image Dockerfile: https://raw.githubusercontent.com/grafana/loki/v3.7.8/clients/cmd/fluentd/Dockerfile — база fluent/fluentd:v1.19-debian-1.
- Fluentd parser: https://docs.fluentd.org/filter/parser — reserve_data/reserve_time/remove_key_name_field.
- Grafana release: https://grafana.com/docs/grafana/latest/whatsnew/whats-new-in-v13-1/

Доступность веб-страницы релиза не равна проверенному image pull. Поэтому конкретные выбранные container tags требуют `make lock`, проверок выбранных контейнеров и настоящего deployment. Тестирование всех комбинаций не заявляется на основании одной таблицы совместимости.
