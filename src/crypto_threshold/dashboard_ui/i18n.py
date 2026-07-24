"""Small bilingual message catalog for the Crypto Threshold dashboard."""

from __future__ import annotations

DEFAULT_LANG = "zh"
SUPPORTED_LANGS = frozenset({"zh", "en"})

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "app.title": "Crypto Threshold Research",
        "app.classification": "Research prototype",
        "app.no_go": "Live status: NO-GO",
        "nav.overview": "Overview",
        "nav.markets": "Markets",
        "nav.calibration": "Calibration",
        "nav.paper": "Paper Ledger",
        "nav.shadow": "Shadow",
        "nav.readiness": "Readiness",
        "nav.wallet": "Wallet",
        "language.zh": "中文",
        "language.en": "English",
        "common.no_rows": "No rows yet.",
        "common.configured": "Configured",
        "common.not_configured": "Not configured",
        "common.save": "Save local configuration",
        "overview.title": "Research control plane",
        "overview.subtitle": (
            "Public market data, calibration, shadow evidence, "
            "and paper accounting only."
        ),
        "overview.counts": "Evidence inventory",
        "overview.top_markets": "Highest current net EV",
        "overview.paper": "Paper summary",
        "overview.latest_shadow": "Latest shadow cycles",
        "markets.title": "BTC / ETH threshold markets",
        "markets.detail": "Market evidence",
        "calibration.title": "Replay and calibration",
        "paper.title": "Paper ledger",
        "shadow.title": "Shadow monitoring",
        "readiness.title": "Dashboard safety checks",
        "readiness.scope": (
            "These startup and isolation checks are not Phase 2 empirical acceptance."
        ),
        "readiness.required_gates": "Required dashboard safety gates",
        "wallet.title": "Wallet configuration",
        "wallet.help": (
            "The private key is stored only in macOS Keychain. "
            "It is not connected to signing or orders."
        ),
        "wallet.private_status": "Private key status",
        "wallet.private_key": "New private key",
        "wallet.blank_keep": "Leave blank to keep the current Keychain item.",
        "wallet.funder": "Funder address",
        "wallet.signer": "Derived signer address",
        "wallet.derive": "Derive funder from the new private key",
        "wallet.delete": "Delete the saved private key",
        "wallet.keychain": "Keychain",
        "wallet.safety": "TRADING_DISABLED remains true. No SecureClient is created by this page.",
        "wallet.public_disabled": (
            "Wallet setup is available only on a loopback-only local dashboard."
        ),
        "flash.wallet_saved": "Wallet configuration saved locally.",
        "flash.error": "Request rejected",
        "error.not_found": "Page not found",
        "error.unknown_page": "Unknown dashboard page.",
        "error.invalid_post": "This dashboard action is not supported.",
    },
    "zh": {
        "app.title": "Crypto Threshold 研究台",
        "app.classification": "研究原型",
        "app.no_go": "实盘状态：NO-GO",
        "nav.overview": "概览",
        "nav.markets": "市场",
        "nav.calibration": "校准",
        "nav.paper": "Paper 账本",
        "nav.shadow": "Shadow",
        "nav.readiness": "就绪检查",
        "nav.wallet": "钱包",
        "language.zh": "中文",
        "language.en": "English",
        "common.no_rows": "暂无记录。",
        "common.configured": "已配置",
        "common.not_configured": "未配置",
        "common.save": "保存本地配置",
        "overview.title": "研究控制台",
        "overview.subtitle": "仅展示公共市场数据、校准、shadow 证据和 paper 账本。",
        "overview.counts": "证据库存",
        "overview.top_markets": "当前净 EV 排名",
        "overview.paper": "Paper 汇总",
        "overview.latest_shadow": "最近 Shadow 周期",
        "markets.title": "BTC / ETH 阈值市场",
        "markets.detail": "市场证据",
        "calibration.title": "Replay 与校准",
        "paper.title": "Paper 账本",
        "shadow.title": "Shadow 监控",
        "readiness.title": "Dashboard 安全检查",
        "readiness.scope": "这些启动与隔离检查不代表 Phase 2 经验验收通过。",
        "readiness.required_gates": "Dashboard 必需安全门槛",
        "wallet.title": "钱包配置",
        "wallet.help": "私钥只保存在 macOS Keychain；当前不会连接签名或订单。",
        "wallet.private_status": "私钥状态",
        "wallet.private_key": "新私钥",
        "wallet.blank_keep": "留空表示保留现有 Keychain 项。",
        "wallet.funder": "Funder 地址",
        "wallet.signer": "推导出的 signer 地址",
        "wallet.derive": "用新私钥推导 funder",
        "wallet.delete": "删除已保存私钥",
        "wallet.keychain": "Keychain",
        "wallet.safety": "TRADING_DISABLED 始终保持 true，本页面不会创建 SecureClient。",
        "wallet.public_disabled": "钱包配置仅在 loopback 本地 Dashboard 中可用。",
        "flash.wallet_saved": "钱包配置已保存到本地。",
        "flash.error": "请求被拒绝",
        "error.not_found": "页面不存在",
        "error.unknown_page": "未知 Dashboard 页面。",
        "error.invalid_post": "不支持此 Dashboard 操作。",
    },
}


def t(lang: str, key: str, **kwargs: object) -> str:
    selected = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    template = MESSAGES[selected].get(key) or MESSAGES["en"].get(key) or key
    return template.format(**kwargs) if kwargs else template
