# Опубликованные измерения

Оба прогона полностью прошли на исходном commit `f99de235d92b8909b59827021f41d97178e8d07b`.
Это сохранённые результаты перед финализацией паспорта. Финальный commit документов
повторно проверяется workflow Ubuntu kubeadm acceptance перед упаковкой;
свежий отчёт находится в его Actions artifact и записывает точный HEAD.

- Ubuntu: https://github.com/GeKosGaming/signal-devops-mtc-engineer-hack/actions/runs/37145316744
- kind CI: https://github.com/GeKosGaming/signal-devops-mtc-engineer-hack/actions/runs/37145307254

В каждом JSON указаны ОС, commit, чистота рабочей копии и реальные проверки.
Operations report включает конкурирующие команды и восстановление после SIGINT/SIGTERM.
Перед публикацией просмотрены только JSON/HTML reports и версии пакетов ОС.
Credentials, kubeconfig, private keys, `.state/` и диагностические логи не включены.
Счётчики Nginx включают probes/scrapes, а задержки Loki включают polling;
результаты одного лабораторного запуска не объявляются production SLA или exactly-once.
