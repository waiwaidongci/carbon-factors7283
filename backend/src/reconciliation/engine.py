"""两期差异归因核心引擎（纯标准库，Decimal 精确计算）。

口径
----
1. 两份报告的冻结底稿按"冻结的连续编号"配对；单边缺项按零用量，
   因子与部门借用有记录一侧；缺编号/重号/单位不可比直接拒算。
2. 每条配对在三个维度上发生变动：用量 Q、因子 F、部门归属 D。
   状态 S（已从基准替换为对比的维度集合）下：
       q(S) = q2 if Q∈S else q1； f(S) 同理； dept(S) 同理
       排放 E(S) = q(S)·f(S)，记在 dept(S) 名下。
   对部门 d 定义 e_d(S) = E(S) if dept(S)==d else 0。
   维度 k 的 Shapley 边际（对固定部门 d 比较前后状态）：
       φ_{d,k} = 对全部 3!=6 种替换顺序取平均 [e_d(S∪{k}) − e_d(S)]
3. 所有部门、三个维度的贡献之和严格等于原两期报告总排放差额。

数值处理：全程 Decimal，边际贡献先按 6 倍分子累加（批处理只做加法），
最终再除以 6 并统一量化，残差按确定性规则分配，保证分毫不差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from itertools import permutations
from typing import Optional

# 与 src.services.calculation 保持一致的显式单位换算表
CONVERSIONS = {
    ('MWh', 'kWh'): Decimal('1000'),
    ('kWh', 'kWh'): Decimal('1'),
    ('L', 'L'): Decimal('1'),
    ('mL', 'L'): Decimal('0.001'),
    ('km', 'km'): Decimal('1'),
    ('m', 'km'): Decimal('0.001'),
}

Q, F, D = 'quantity', 'factor', 'department'
DIMENSIONS = (Q, F, D)
DIMENSION_LABELS = {Q: '用量变动', F: '因子变动', D: '部门归属变动'}

# 三元素集合全部 6 种替换顺序
ORDERS = tuple(permutations(DIMENSIONS))
ORDER_COUNT = Decimal(len(ORDERS))  # 6

REJECT_MISSING_NUMBER = 'missing_number'
REJECT_DUPLICATE_NUMBER = 'duplicate_number'
REJECT_UNIT_INCOMPARABLE = 'unit_incomparable'
REJECT_REPORT_STATE = 'report_state'


class RejectError(Exception):
    """拒算：编号、单位或报告状态不满足归因前置条件。"""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class FrozenLine:
    """一条冻结底稿的标准化记录（用量已换算到因子单位）。"""

    line_no: int
    activity: str
    unit: str                 # 因子单位（换算后）
    source_unit: str          # 原始录入单位
    quantity: Decimal         # 换算后的统一活动量
    factor: Decimal           # kg_per_unit（冻结）
    emission: Decimal         # quantity × factor
    department: str


@dataclass
class PairData:
    """同一冻结编号在两期报告中的配对。"""

    line_no: int
    activity: str
    unit: str
    q1: Decimal
    f1: Decimal
    q2: Decimal
    f2: Decimal
    dept1: str
    dept2: str
    missing_side: Optional[str]  # None / 'base' / 'current'

    @property
    def e1(self) -> Decimal:
        return self.q1 * self.f1

    @property
    def e2(self) -> Decimal:
        return self.q2 * self.f2

    def state(self, active: set):
        """返回替换集合 active 下的 (用量, 因子, 部门, 排放)。"""
        q = self.q2 if Q in active else self.q1
        f = self.f2 if F in active else self.f1
        dept = self.dept2 if D in active else self.dept1
        return q, f, dept, q * f


@dataclass
class PairContribution:
    """单条配对的归因结果（6 倍分子，未除 6）。"""

    line_no: int
    # (部门, 维度) -> 6 倍边际贡献分子
    numerators: dict = field(default_factory=dict)
    delta: Decimal = Decimal(0)

    def add(self, department: str, dimension: str, value: Decimal) -> None:
        if value == 0:
            return
        key = (department, dimension)
        self.numerators[key] = self.numerators.get(key, Decimal(0)) + value


def to_decimal(value) -> Decimal:
    """禁止经过 float 二进制误差：数值一律经 str 转 Decimal。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def normalized_quantity(quantity, source_unit: str, factor_unit: str) -> Decimal:
    key = (source_unit, factor_unit)
    if key not in CONVERSIONS:
        raise RejectError(
            REJECT_UNIT_INCOMPARABLE,
            f'单位不可比：{source_unit} → {factor_unit} 不在显式换算表内',
            {'source_unit': source_unit, 'factor_unit': factor_unit},
        )
    return to_decimal(quantity) * CONVERSIONS[key]


def build_frozen_line(raw: dict) -> FrozenLine:
    """把冻结快照中的一条底稿标准化为 FrozenLine。

    raw 必须含冻结编号 line_no；因子取报告冻结口径。
    """
    if 'line_no' not in raw or isinstance(raw.get('line_no'), bool):
        raise RejectError(
            REJECT_MISSING_NUMBER,
            '底稿缺少冻结编号 line_no，无法按连续编号配对',
            {'raw_keys': sorted(raw.keys())},
        )
    try:
        line_no = int(raw['line_no'])
    except (TypeError, ValueError):
        raise RejectError(
            REJECT_MISSING_NUMBER,
            f"冻结编号不是整数：{raw['line_no']!r}",
            {'line_no': raw.get('line_no')},
        )
    factor_unit = raw['factor_unit']
    factor = to_decimal(raw['kg_per_unit'])
    quantity = normalized_quantity(raw['quantity'], raw['unit'], factor_unit)
    return FrozenLine(
        line_no=line_no,
        activity=raw['activity'],
        unit=factor_unit,
        source_unit=raw['unit'],
        quantity=quantity,
        factor=factor,
        emission=quantity * factor,
        department=raw['department'],
    )


def validate_numbering(lines, label: str) -> None:
    """单侧编号校验：非空、无重号（连续性在两侧并集上校验，允许单边缺项）。"""
    seen = set()
    for line in lines:
        if line.line_no in seen:
            raise RejectError(
                REJECT_DUPLICATE_NUMBER,
                f'{label}存在重号：{line.line_no}',
                {'report': label, 'line_no': line.line_no},
            )
        seen.add(line.line_no)
    if not seen:
        raise RejectError(REJECT_MISSING_NUMBER, f'{label}没有任何底稿行',
                          {'report': label})


def validate_pair_numbering(base_lines, current_lines) -> None:
    """配对编号总校验：两侧各自无重号，并集必须是从 1 起的连续编号。

    允许单边缺项（某编号只在一侧）；并集出现缺口或缺编号即拒算。
    """
    validate_numbering(base_lines, '基准报告')
    validate_numbering(current_lines, '对比报告')
    union = {x.line_no for x in base_lines} | {x.line_no for x in current_lines}
    expect = set(range(1, len(union) + 1))
    missing = sorted(expect - union)
    if missing:
        raise RejectError(
            REJECT_MISSING_NUMBER,
            f'两期编号并集未从 1 连续编号，缺号：{missing}',
            {'missing': missing},
        )


def build_pairs(base_lines, current_lines) -> list:
    """按冻结连续编号配对两期底稿。"""
    validate_numbering(base_lines, '基准报告')
    validate_numbering(current_lines, '对比报告')
    base_map = {line.line_no: line for line in base_lines}
    current_map = {line.line_no: line for line in current_lines}

    pairs = []
    for no in sorted(set(base_map) | set(current_map)):
        a = base_map.get(no)
        b = current_map.get(no)
        if a is not None and b is not None:
            if a.unit != b.unit or a.activity != b.activity:
                raise RejectError(
                    REJECT_UNIT_INCOMPARABLE,
                    f'编号 {no} 两期单位/活动不可比：'
                    f'{a.activity}({a.unit}) vs {b.activity}({b.unit})',
                    {'line_no': no, 'base': f'{a.activity}:{a.unit}',
                     'current': f'{b.activity}:{b.unit}'},
                )
            pairs.append(PairData(
                line_no=no, activity=a.activity, unit=a.unit,
                q1=a.quantity, f1=a.factor, q2=b.quantity, f2=b.factor,
                dept1=a.department, dept2=b.department, missing_side=None,
            ))
        elif b is not None:  # 基准侧缺项：零用量，借用对比侧因子/部门
            pairs.append(PairData(
                line_no=no, activity=b.activity, unit=b.unit,
                q1=Decimal(0), f1=b.factor, q2=b.quantity, f2=b.factor,
                dept1=b.department, dept2=b.department, missing_side='base',
            ))
        else:  # 对比侧缺项：零用量，借用基准侧因子/部门
            pairs.append(PairData(
                line_no=no, activity=a.activity, unit=a.unit,
                q1=a.quantity, f1=a.factor, q2=Decimal(0), f2=a.factor,
                dept1=a.department, dept2=a.department, missing_side='current',
            ))
    return pairs


def attribute_pair(pair: PairData) -> PairContribution:
    """枚举 6 种替换顺序，按部门得失记平均边际贡献（返回 6 倍分子）。

    每一步 k 对固定部门 d 比较 e_d(S∪{k})−e_d(S)：
      * k∈{Q,F}：部门不变，该部门承担排放增量 E(S')−E(S)；
      * k=D：部门归属转移，旧部门 −E(S)、新部门 +E(S')。
    """
    out = PairContribution(line_no=pair.line_no, delta=pair.e2 - pair.e1)
    for order in ORDERS:
        active: set = set()
        for dimension in order:
            _, _, dept_before, emission_before = pair.state(active)
            active.add(dimension)
            _, _, dept_after, emission_after = pair.state(active)
            if dimension == D:
                out.add(dept_before, D, -emission_before)
                out.add(dept_after, D, emission_after)
            else:
                out.add(dept_after, dimension, emission_after - emission_before)
    return out


class Accumulator:
    """分批累加器：内存中只保留 (部门,维度) 与总量级个 Decimal。"""

    def __init__(self):
        self.numerators: dict = {}       # (dept, dim) -> 6 倍分子之和
        self.total_base = Decimal(0)
        self.total_current = Decimal(0)
        self.pair_count = 0
        self.missing_base = 0
        self.missing_current = 0

    def add_pair(self, pair: PairData) -> PairContribution:
        contrib = attribute_pair(pair)
        for key, value in contrib.numerators.items():
            self.numerators[key] = self.numerators.get(key, Decimal(0)) + value
        self.total_base += pair.e1
        self.total_current += pair.e2
        self.pair_count += 1
        self.missing_base += 1 if pair.missing_side == 'base' else 0
        self.missing_current += 1 if pair.missing_side == 'current' else 0
        return contrib

    def merge(self, other: 'Accumulator') -> None:
        for key, value in other.numerators.items():
            self.numerators[key] = self.numerators.get(key, Decimal(0)) + value
        self.total_base += other.total_base
        self.total_current += other.total_current
        self.pair_count += other.pair_count
        self.missing_base += other.missing_base
        self.missing_current += other.missing_current


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum)


def finalize(acc: Accumulator, places: int = 6,
             original_base: Optional[Decimal] = None,
             original_current: Optional[Decimal] = None) -> dict:
    """除以 6、统一量化并做残差分配，输出完整对账结果。"""
    quantum = Decimal(1).scaleb(-places)
    with localcontext() as ctx:
        ctx.prec = 50
        exact = {key: value / ORDER_COUNT for key, value in sorted(acc.numerators.items())}

        ob = acc.total_base if original_base is None else to_decimal(original_base)
        oc = acc.total_current if original_current is None else to_decimal(original_current)
        report_delta = _quantize(oc - ob, quantum)
        target_delta = _quantize(acc.total_current - acc.total_base, quantum)

        quantized = {key: _quantize(value, quantum) for key, value in exact.items()}
        residual = target_delta - sum(quantized.values(), Decimal(0))
        # 确定性残差分配：按精确值大小与键序，逐最小粒度补给/扣减
        if residual != 0 and exact:
            units = int((residual / quantum).to_integral_value())
            order = sorted(exact, key=lambda k: (exact[k], k), reverse=(units > 0))
            idx = 0
            while units != 0:
                key = order[idx % len(order)]
                quantized[key] += quantum if units > 0 else -quantum
                units += -1 if units > 0 else 1
                idx += 1
        elif residual != 0:
            # 无任何贡献键却存在差额：数据异常，交由调用方门控拦截
            raise RejectError(
                'tie_out_failed', '归因贡献为空但存在差额，无法分配',
                {'residual': str(residual)})

        components_total = sum(quantized.values(), Decimal(0))
        zero = Decimal(0).quantize(quantum)
        by_department = []
        for dept in sorted({k[0] for k in quantized}):
            row = {'department': dept}
            row_total = Decimal(0)
            for dim in DIMENSIONS:
                value = quantized.get((dept, dim), zero)
                row[DIMENSION_LABELS[dim]] = str(value)
                row_total += value
            row['total'] = str(row_total)
            by_department.append(row)

        return {
            'places': places,
            'original_base_total': str(_quantize(ob, quantum)),
            'original_current_total': str(_quantize(oc, quantum)),
            'recomputed_base_total': str(_quantize(acc.total_base, quantum)),
            'recomputed_current_total': str(_quantize(acc.total_current, quantum)),
            'report_delta': str(report_delta),
            'delta': str(target_delta),
            'components': [
                {'department': dept, 'dimension': dim,
                 'dimension_label': DIMENSION_LABELS[dim], 'value': str(value)}
                for (dept, dim), value in sorted(quantized.items())
            ],
            'by_department': by_department,
            'components_total': str(components_total),
            'tie_out_ok': components_total == target_delta == report_delta,
            'pair_count': acc.pair_count,
            'missing_base': acc.missing_base,
            'missing_current': acc.missing_current,
            'method': '三维度（用量/因子/部门归属）全部6种替换顺序的平均边际贡献；'
                      'Decimal精确累加后统一量化，残差确定性分配，贡献之和等于原报告差额。',
        }
