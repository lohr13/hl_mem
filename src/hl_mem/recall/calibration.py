"""排序信号到 P(relevant) 的轻量逻辑回归校准。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CalibrationModel:
    """可序列化的二分类逻辑回归模型。"""

    feature_names: tuple[str, ...]
    weights: tuple[float, ...]
    intercept: float

    def predict(self, features: dict[str, float]) -> float:
        """返回经过 sigmoid 映射的相关概率。"""
        value = self.intercept + sum(
            weight * float(features.get(name, 0.0)) for name, weight in zip(self.feature_names, self.weights)
        )
        value = max(-35.0, min(35.0, value))
        return 1.0 / (1.0 + math.exp(-value))

    def save(self, path: Path) -> None:
        """将模型写为稳定 JSON。"""
        path.write_text(
            json.dumps(
                {
                    "feature_names": self.feature_names,
                    "weights": self.weights,
                    "intercept": self.intercept,
                }
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "CalibrationModel":
        """从 JSON 加载模型。"""
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            tuple(payload["feature_names"]),
            tuple(map(float, payload["weights"])),
            float(payload["intercept"]),
        )


def fit_logistic(
    rows: list[tuple[dict[str, float], int]],
    *,
    iterations: int = 1000,
    learning_rate: float = 0.1,
) -> CalibrationModel:
    """使用批量梯度下降拟合小型逻辑回归校准器。"""
    if not rows:
        raise ValueError("calibration rows must not be empty")
    names = tuple(sorted({name for features, _ in rows for name in features}))
    weights = [0.0] * len(names)
    intercept = 0.0
    for _ in range(iterations):
        weight_gradients = [0.0] * len(names)
        intercept_gradient = 0.0
        for features, label in rows:
            score = intercept + sum(weights[index] * features.get(name, 0.0) for index, name in enumerate(names))
            probability = 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, score))))
            error = probability - label
            intercept_gradient += error
            for index, name in enumerate(names):
                weight_gradients[index] += error * features.get(name, 0.0)
        scale = learning_rate / len(rows)
        intercept -= scale * intercept_gradient
        weights = [weight - scale * gradient for weight, gradient in zip(weights, weight_gradients)]
    return CalibrationModel(names, tuple(weights), intercept)
