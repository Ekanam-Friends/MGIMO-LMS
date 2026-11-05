"""Telegram-бот кабинета (решение docs/decisions/0005-telegram-bot.md).

Модули без aiogram (`link`, `ics`, `formatting`, `notify`) импортирует и веб-приложение;
aiogram нужен только `handlers`, `scheduler` и `main`.
"""
