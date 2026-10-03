# Опубликованные измерения

Оба прогона полностью прошли на исходном commit `efa05ee18b02717f7ff6a54b9cf8bfa22ac9daec`.
Это сохранённые результаты перед финализацией паспорта. Финальный commit документов
повторно проверяется workflow Ubuntu kubeadm acceptance перед упаковкой;
свежий отчёт находится в его Actions artifact и записывает точный HEAD.

- Ubuntu: https://github.com/GeKosGaming/signal-devops-mtc-engineer-hack/actions/runs/37141476784
- kind CI: https://github.com/GeKosGaming/signal-devops-mtc-engineer-hack/actions/runs/37141475487

В каждом JSON указаны ОС, commit, чистота рабочей копии и реальные проверки.
Перед публикацией просмотрены только JSON/HTML reports и версии пакетов ОС.
Credentials, kubeconfig, private keys, `.state/` и диагностические логи не включены.
Счётчики Nginx включают probes/scrapes, а задержки Loki включают polling;
результаты одного лабораторного запуска не объявляются production SLA или exactly-once.
