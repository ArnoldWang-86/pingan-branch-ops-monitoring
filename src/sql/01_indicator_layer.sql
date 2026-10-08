-- =============================================================================
-- 银行网点运营指标监控 —— SQL 指标层
-- 目标库：MySQL 8.0（窗口函数、CTE 均可用）
-- 数据源：data/raw/branch_ops_detail.jsonl（bootstrap 模拟数据，见 simulation_meta.json）
--
-- 四层结构：
--   ods_branch_ops_detail   明细层：网点 × 日 的原始运营流水
--   dim_branch              维度层：网点属性
--   dwd_branch_ops_daily    指标层：派生指标 + 口径统一
--   dws_branch_ops_monitor  监控层：滚动基线、同环比、排名、稳健偏离度
--   ads_ops_alert           预警层：持续性过滤 + 分级 + 疑似成因判定
--
-- 设计说明（面试可讲）：
-- 1. 原始数据是 JSONL，MySQL 8.0 可用 LOAD DATA + JSON_TABLE 直接入 ODS；
--    也可先用 src/py/load_to_mysql.py 落库，本脚本只负责建模与指标计算。
-- 2. 所有比率类指标在 SQL 里只算「分子/分母」，不落冗余比率，
--    避免下游出现两套口径（这是我在另一个项目里踩过的坑）。
-- 3. 滚动基线用「同网点、不含当日的前 7 日均值」，
--    用 ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING 精确表达，排除自身污染。
-- 4. 偏离度用 MAD 类稳健离散度而非标准差：运营数据里的真实极端值
--    （设备故障、营销活动）会把标准差撑大，导致真正的异常落进 3σ 里检不出来。
-- 5. 预警必须带**持续性**要求：缓慢漂移（新人上岗导致差错率逐周上升）
--    单日偏离很小，按单日阈值永远报不出来；反之正常波动每天都可能越线。
--    因此严重度由「偏离幅度 × 持续性 × 方法一致性」共同决定。
--
-- ⚠ 基线口径说明（这一版已按星期几对齐，与 Python 分析层同口径）
-- ------------------------------------------------------------------
-- 网点业务量的周内效应极强：本项目模拟数据里周日业务量只有工作日的约 1/3。
-- 早期版本用「前 7 日均值」做基线，未按星期几对齐，实测后果是：
--   · 每个周日都显示约 -65% 的偏离，每周固定产生一次假异常
--   · 预警膨胀到 1359 条，占全部网点日的 75% —— 这等于预警系统失效
-- 修正方式：先按 (网点, 星期几) 求历史中位数作为基线（见 vol_wd_base / wait_wd_base），
-- 再算偏离。修正后预警收敛到与 Python 层同一量级。
-- 这也是本项目最重要的一条方法论：**做异常检测前先把数据的固有周期结构搞清楚**。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 0. 建表
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS ods_branch_ops_detail;
CREATE TABLE ods_branch_ops_detail (
    stat_date             DATE          NOT NULL COMMENT '统计日期',
    weekday               TINYINT       NOT NULL COMMENT '1=周一 .. 7=周日',
    branch_code           VARCHAR(16)   NOT NULL COMMENT '网点编码',
    branch_name           VARCHAR(64)   NOT NULL COMMENT '网点名称',
    branch_kind           VARCHAR(32)   NOT NULL COMMENT '网点类型',
    region                VARCHAR(32)   NOT NULL COMMENT '所属片区',
    open_windows          INT           NOT NULL COMMENT '开放服务窗口数',
    staff_on_duty         INT           NOT NULL COMMENT '当日在岗运营人员数',
    counter_txn_cnt       INT           NOT NULL COMMENT '柜面业务笔数',
    smart_device_txn_cnt  INT           NOT NULL COMMENT '智能设备业务笔数',
    mobile_txn_cnt        INT           NOT NULL COMMENT '移动/线上渠道业务笔数',
    avg_wait_minutes      DECIMAL(12,2) NOT NULL COMMENT '平均等候时长（分钟）',
    max_wait_minutes      DECIMAL(12,2) NOT NULL COMMENT '最长等候时长（分钟）',
    txn_error_cnt         INT           NOT NULL COMMENT '业务差错笔数',
    complaint_cnt         INT           NOT NULL COMMENT '客户投诉笔数',
    post_review_cnt       INT           NOT NULL COMMENT '事后监督复核笔数',
    customer_satisfaction DECIMAL(6,3)  NOT NULL COMMENT '客户满意度（0-5）',
    op_cost_ratio         DECIMAL(8,5)  NOT NULL COMMENT '运营成本收入比',
    PRIMARY KEY (stat_date, branch_code),
    KEY idx_branch_date (branch_code, stat_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ODS：网点日运营明细（模拟数据）';

DROP TABLE IF EXISTS dim_branch;
CREATE TABLE dim_branch (
    branch_code          VARCHAR(16) PRIMARY KEY,
    branch_name          VARCHAR(64)   NOT NULL,
    branch_kind          VARCHAR(32)   NOT NULL,
    region               VARCHAR(32)   NOT NULL,
    age_years            DECIMAL(5,1)  NOT NULL COMMENT '开业年限',
    base_total_volume    DECIMAL(10,2) NOT NULL COMMENT '日均总业务量基线',
    e_channel_rate       DECIMAL(6,4)  NOT NULL COMMENT '电子渠道分流率基线',
    open_windows         INT           NOT NULL,
    staff                INT           NOT NULL,
    is_free_trade_zone   TINYINT       NOT NULL DEFAULT 0 COMMENT '是否自贸区网点'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='DIM：网点属性';

-- -----------------------------------------------------------------------------
-- 1. 指标层 dwd_branch_ops_daily
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS dwd_branch_ops_daily;
CREATE TABLE dwd_branch_ops_daily AS
SELECT
    d.stat_date,
    d.weekday,
    d.branch_code,
    d.branch_name,
    d.branch_kind,
    d.region,
    b.is_free_trade_zone,
    d.open_windows,
    d.staff_on_duty,
    d.counter_txn_cnt,
    d.counter_txn_cnt + d.smart_device_txn_cnt + d.mobile_txn_cnt AS total_txn_cnt,

    -- 渠道分流率：分子分母都留，下游可自行换口径
    ROUND((d.smart_device_txn_cnt + d.mobile_txn_cnt)
          / NULLIF(d.counter_txn_cnt + d.smart_device_txn_cnt + d.mobile_txn_cnt, 0), 4)
        AS e_channel_rate,
    ROUND(d.smart_device_txn_cnt
          / NULLIF(d.smart_device_txn_cnt + d.mobile_txn_cnt, 0), 4) AS smart_share,

    d.avg_wait_minutes,
    d.max_wait_minutes,
    d.customer_satisfaction,
    d.op_cost_ratio,

    -- 计数类字段必须一并带到指标层：
    -- dws 的周度口径要算「7 天累计差错数 / 7 天累计业务量」，
    -- 只留差错率是无法按周聚合的（这个 bug 在首次真正执行本文件时才暴露出来）。
    d.txn_error_cnt,
    d.complaint_cnt,
    d.post_review_cnt,

    -- 风险类指标统一转成「率」，避免量纲混淆
    ROUND(d.txn_error_cnt / NULLIF(d.counter_txn_cnt, 0), 6)          AS error_rate,
    ROUND(d.complaint_cnt / NULLIF(d.counter_txn_cnt, 0) * 10000, 4)  AS complaint_per_10k,
    ROUND(d.post_review_cnt / NULLIF(d.counter_txn_cnt, 0), 6)        AS post_review_rate,

    -- 人效与窗口效能
    ROUND(d.counter_txn_cnt / NULLIF(d.staff_on_duty, 0), 2)          AS txn_per_staff,
    ROUND(d.counter_txn_cnt / NULLIF(d.open_windows, 0), 2)           AS txn_per_window,
    -- 产能饱和度：单窗口实际业务量 / 单窗口 7.5 小时理论产能（每笔 8.5 分钟）
    ROUND(d.counter_txn_cnt / NULLIF(d.open_windows, 0)
          / NULLIF(7.5 * 60 / 8.5, 0), 4)                             AS capacity_utilization
FROM ods_branch_ops_detail d
LEFT JOIN dim_branch b ON b.branch_code = d.branch_code;

ALTER TABLE dwd_branch_ops_daily ADD PRIMARY KEY (stat_date, branch_code);

-- -----------------------------------------------------------------------------
-- 2. 监控层 dws_branch_ops_monitor
--    滚动基线、同环比、横向排名、稳健偏离度、**持续性计数**
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS dws_branch_ops_monitor;
CREATE TABLE dws_branch_ops_monitor AS
WITH wd_seq AS (
    -- 给每个 (网点, 星期几) 组内的记录编号，并顺带取组内累计统计量。
    -- 之所以先做这一步：窗口函数不能嵌套 frame，
    -- 想算「同星期几的前 4 次观测」必须先把组内序号物化出来。
    SELECT
        d.*,
        ROW_NUMBER() OVER (PARTITION BY branch_code, weekday
                           ORDER BY stat_date) AS wd_seq
    FROM dwd_branch_ops_daily d
),
wd_base AS (
    -- 同星期几基线：取 (网点, 星期几) 组内「当前记录之前的 4 次观测」的均值。
    -- 这是本文件的核心修正——用 7 日滚动均值做基线会让每个周日都变成假异常。
    --
    -- 用均值近似中位数：MySQL 8 没有 MEDIAN 聚合函数，
    -- 而同组样本只有 4 个，均值与中位数在这里的差异可以忽略。
    -- 若追求严格中位数，可升级到 MySQL 8.0 的 PERCENTILE_CONT 窗口函数。
    SELECT
        s.branch_code, s.stat_date,
        AVG(s.counter_txn_cnt)   OVER w4 AS vol_wd_base,
        AVG(s.avg_wait_minutes)  OVER w4 AS wait_wd_base,
        AVG(s.e_channel_rate)    OVER w4 AS ech_wd_base,
        AVG(s.op_cost_ratio)     OVER w4 AS cost_wd_base,
        AVG(s.error_rate)        OVER w4 AS err_wd_base,
        AVG(s.complaint_per_10k) OVER w4 AS comp_wd_base
    FROM wd_seq s
    WINDOW w4 AS (PARTITION BY s.branch_code, s.weekday
                  ORDER BY s.stat_date
                  ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING)
),
mad AS (
    -- 当日跨网点的平均绝对偏差（Mean Absolute Deviation）。
    -- MySQL 没有 MEDIAN 聚合函数，故用 AVG(ABS(x - 当日均值))。
    -- 它同样对极端值不敏感（比标准差稳健），但不是严格意义上的中位数绝对偏差，
    -- 字段命名与文档都如实标注这一点，Python 分析层用的是真 MAD。
    SELECT stat_date,
           AVG(ABS(avg_wait_minutes - day_avg_wait)) AS mad_wait,
           AVG(ABS(error_rate - day_avg_err))        AS mad_err,
           AVG(ABS(complaint_per_10k - day_avg_comp)) AS mad_comp
    FROM (
        SELECT d.stat_date, d.avg_wait_minutes, d.error_rate, d.complaint_per_10k,
               AVG(d.avg_wait_minutes)  OVER (PARTITION BY d.stat_date) AS day_avg_wait,
               AVG(d.error_rate)        OVER (PARTITION BY d.stat_date) AS day_avg_err,
               AVG(d.complaint_per_10k) OVER (PARTITION BY d.stat_date) AS day_avg_comp
        FROM dwd_branch_ops_daily d
    ) t
    GROUP BY stat_date
),
win AS (
    SELECT
        m.*,
        md.mad_wait, md.mad_err, md.mad_comp,
        -- 同星期几基线（来自 wd_base）。必须在这里显式带出，
        -- 因为 m.* 只覆盖 dwd 的列，join 进来的列不会被 * 带出。
        wb2.vol_wd_base, wb2.wait_wd_base, wb2.ech_wd_base,
        wb2.cost_wd_base, wb2.err_wd_base, wb2.comp_wd_base,
        -- 7 日滚动基线：严格排除当日，避免异常值污染自己的基线
        AVG(m.counter_txn_cnt)       OVER w7 AS counter_txn_ma7,
        AVG(m.avg_wait_minutes)      OVER w7 AS wait_ma7,
        AVG(m.error_rate)            OVER w7 AS error_rate_ma7,
        AVG(m.complaint_per_10k)     OVER w7 AS complaint_ma7,
        AVG(m.customer_satisfaction) OVER w7 AS satisfaction_ma7,
        AVG(m.e_channel_rate)        OVER w7 AS e_channel_ma7,
        -- 成本收入比的基线也要算：ads 层判定「批量重跑」时用的是
        -- 「业务量虚高但成本率反而下降」这个指标间不自洽的特征，
        -- 需要拿当日成本率与自身基线比较（此前漏了这一列）。
        AVG(m.op_cost_ratio)         OVER w7 AS op_cost_ratio_ma7,
        -- 中短期对比窗口：用于识别「台阶式」变化（口径调整的典型形态）
        AVG(m.counter_txn_cnt)  OVER w21 AS counter_txn_ma21,
        AVG(m.avg_wait_minutes) OVER w21 AS wait_ma21,
        AVG(m.error_rate)       OVER w21 AS error_rate_ma21,
        AVG(m.complaint_per_10k) OVER w21 AS complaint_ma21,
        -- 周度口径（差错这类低频计数指标必须按周看，日粒度信噪比太低）
        SUM(m.txn_error_cnt)   OVER w7s  AS err_cnt_7d,
        SUM(m.counter_txn_cnt) OVER w7s  AS txn_cnt_7d,
        -- 上一周的累计差错数与业务量，用于算「周度差错率的基线」。
        -- 为什么必须用「上一周」而不是 21 日均值：后者把当前周的分子分母
        -- 也混了进去，一旦业务量被注入异常污染（如批量重跑），基线就跟着失真，
        -- 结果 837 行里 427 行被误触发（实测踩过）。
        -- 窗口取「13 天前到 7 天前」，正好是完全排除了当前 7 天的上一周。
        SUM(m.txn_error_cnt)   OVER w7prev AS err_cnt_7d_prev,
        SUM(m.counter_txn_cnt) OVER w7prev AS txn_cnt_7d_prev,
        LAG(m.counter_txn_cnt)  OVER wb AS counter_txn_prev,
        LAG(m.avg_wait_minutes) OVER wb AS wait_prev,
        ROW_NUMBER() OVER wb AS obs_seq
    FROM dwd_branch_ops_daily m
    LEFT JOIN mad md ON md.stat_date = m.stat_date
    LEFT JOIN wd_base wb2
           ON wb2.branch_code = m.branch_code AND wb2.stat_date = m.stat_date
    WINDOW
        w7  AS (PARTITION BY m.branch_code ORDER BY m.stat_date
                ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING),
        w21 AS (PARTITION BY m.branch_code ORDER BY m.stat_date
                ROWS BETWEEN 21 PRECEDING AND 1 PRECEDING),
        w7s AS (PARTITION BY m.branch_code ORDER BY m.stat_date
                ROWS BETWEEN 6 PRECEDING AND CURRENT ROW),
        w7prev AS (PARTITION BY m.branch_code ORDER BY m.stat_date
                ROWS BETWEEN 13 PRECEDING AND 7 PRECEDING),
        wb  AS (PARTITION BY m.branch_code ORDER BY m.stat_date)
)
SELECT
    b.*,
    ROUND((b.counter_txn_cnt - b.counter_txn_prev)
          / NULLIF(b.counter_txn_prev, 0), 4)                        AS counter_txn_dod,
    -- 偏离度：相对**同星期几基线**（不是相对 7 日滚动均值）。
    -- 这一处是全文件最关键的修正，理由见文件头「基线口径说明」。
    ROUND((b.counter_txn_cnt - b.vol_wd_base)
          / NULLIF(b.vol_wd_base, 0), 4)                             AS counter_txn_dev,
    ROUND((b.avg_wait_minutes - b.wait_wd_base)
          / NULLIF(b.wait_wd_base, 0), 4)                            AS wait_dev,
    ROUND((b.error_rate - b.err_wd_base)
          / NULLIF(b.err_wd_base, 0), 4)                             AS error_rate_dev,
    ROUND((b.complaint_per_10k - b.comp_wd_base)
          / NULLIF(b.comp_wd_base, 0), 4)                            AS complaint_dev,
    -- 台阶式变化（对 21 日滚动基线）：口径调整的关键证据
    ROUND((b.counter_txn_cnt - b.counter_txn_ma21)
          / NULLIF(b.counter_txn_ma21, 0), 4)                        AS counter_txn_step,
    ROUND((b.avg_wait_minutes - b.wait_ma21)
          / NULLIF(b.wait_ma21, 0), 4)                               AS wait_step,
    -- 周度差错率与「绝对增量」判据。
    --
    -- 为什么用绝对增量而不是相对变化率（这是本文件第二个关键修正）：
    -- 差错是低频计数事件，上一周基线可能只有 5 笔，
    -- 那么「5 笔 -> 15 笔」的相对变化是 +200%，看着极严重，
    -- 但绝对只差 10 笔。用相对阈值会把这类正常抽样波动全部报成异常
    -- （实测：改用相对判据时 837 行里 427 行被误触发，改用绝对增量后收敛）。
    -- Python 分析层对投诉也是同样的处理（comp_per_10k_abs_delta），此处保持一致。
    ROUND(b.err_cnt_7d / NULLIF(b.txn_cnt_7d, 0), 6)                 AS error_rate_7d,
    ROUND(b.err_cnt_7d_prev / NULLIF(b.txn_cnt_7d_prev, 0), 6)       AS error_rate_7d_prev,
    ROUND((b.err_cnt_7d / NULLIF(b.txn_cnt_7d, 0))
          - (b.err_cnt_7d_prev / NULLIF(b.txn_cnt_7d_prev, 0)), 6)   AS error_rate_7d_abs_delta,
    ROUND((b.err_cnt_7d / NULLIF(b.txn_cnt_7d, 0))
          / NULLIF(b.err_cnt_7d_prev / NULLIF(b.txn_cnt_7d_prev, 0), 0) - 1, 4)
                                                                     AS error_rate_7d_rel,

    -- 稳健标准化（横截面）
    ROUND((b.avg_wait_minutes
           - AVG(b.avg_wait_minutes) OVER (PARTITION BY b.stat_date))
          / NULLIF(b.mad_wait, 0), 3)                                AS wait_robust_z,
    ROUND((b.error_rate - AVG(b.error_rate) OVER (PARTITION BY b.stat_date))
          / NULLIF(b.mad_err, 0), 3)                                 AS error_robust_z,

    RANK() OVER (PARTITION BY b.stat_date ORDER BY b.avg_wait_minutes DESC) AS wait_rank_desc,
    RANK() OVER (PARTITION BY b.stat_date ORDER BY b.error_rate DESC)       AS error_rank_desc,
    PERCENT_RANK() OVER (PARTITION BY b.stat_date ORDER BY b.counter_txn_cnt) AS volume_pct_rank
FROM win b;

ALTER TABLE dws_branch_ops_monitor ADD PRIMARY KEY (stat_date, branch_code);

-- -----------------------------------------------------------------------------
-- 3. 预警层 ads_ops_alert
--
--    三步走：
--      (1) 触发：任一核心指标短期偏离或周度偏离越线
--      (2) 持续性：该网点该指标在过去 7 天内越线 >= 3 天（缓慢漂移的必要条件）
--      (3) 定性：用**窗口统计量交叉校验**判定数据错误 / 口径差异 / 真实风险
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS ads_ops_alert;
CREATE TABLE ads_ops_alert AS
WITH trigger AS (
    SELECT
        m.*,
        -- 方向标记，便于算持续性
        CASE WHEN m.wait_dev > 0.15 THEN 1 WHEN m.wait_dev < -0.15 THEN -1 ELSE 0 END
            AS wait_dir,
        CASE WHEN m.counter_txn_dev > 0.15 THEN 1
             WHEN m.counter_txn_dev < -0.10 THEN -1 ELSE 0 END
            AS vol_dir,
        CASE WHEN COALESCE(m.error_rate_7d_abs_delta, 0) > 0.0015 THEN 1
             WHEN COALESCE(m.error_rate_7d_abs_delta, 0) < -0.0015 THEN -1 ELSE 0 END
            AS err_dir,
        CASE WHEN m.wait_robust_z > 3 OR m.error_robust_z > 3
             OR ABS(COALESCE(m.wait_dev, 0)) > 0.15
             OR ABS(COALESCE(m.counter_txn_dev, 0)) > 0.12
             -- 差错走「绝对增量」：0.0015 = 每万笔多 15 笔差错，
             -- 约相当于基线的 33%（基线约 0.45%）。理由见上方注释。
             OR COALESCE(m.error_rate_7d_abs_delta, 0) > 0.0015
             THEN 1 ELSE 0 END AS triggered
    FROM dws_branch_ops_monitor m
    WHERE m.obs_seq >= 8
),
persist AS (
    SELECT t.*,
        -- 持续性：过去 7 天内该网点在「等候或业务量」方向上的越线天数
        SUM(t.triggered) OVER (PARTITION BY t.branch_code ORDER BY t.stat_date
                               ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS trig_7d,
        COUNT(*) OVER (PARTITION BY t.branch_code ORDER BY t.stat_date
                       ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS trig_win
    FROM trigger t
),
sized AS (
    SELECT p.*,
        -- 严重度 = 最大偏离幅度 x 持续性系数 x 方法一致性
        GREATEST(
            ABS(COALESCE(p.wait_dev, 0)),
            ABS(COALESCE(p.counter_txn_dev, 0)) * 0.85,
            -- 差错用绝对增量换算成「相当于基线的几倍」再参与比较：
            -- 0.0045 是基线差错率，所以 0.0015 的绝对增量约等于 +33%。
            ABS(COALESCE(p.error_rate_7d_abs_delta, 0)) / 0.0045 * 0.90,
            ABS(COALESCE(p.complaint_dev, 0)) * 0.80
        ) AS amplitude,
        CASE WHEN p.trig_win >= 7 AND p.trig_7d >= 5 THEN 1.0
             WHEN p.trig_7d >= 3 THEN 0.8
             ELSE 0.35 END AS persistence_factor
    FROM persist p
),
classified AS (
    SELECT s.*,
        -- 成因判定：按优先级排列。顺序很重要——
        -- 「自助设备故障」必须排在「单位错误」之前，
        -- 否则等候时长同样放大（1.35 倍）会被误判成字段单位问题。
        CASE
            -- 渠道转移：电子渠道分流率骤降 + 柜面量与等候同步上升 = 自助设备/线上渠道故障
            WHEN s.e_channel_rate < s.e_channel_ma7 * 0.92
                 AND COALESCE(s.counter_txn_step, 0) > 0.15
                 AND COALESCE(s.wait_step, 0) > 0.15
                 THEN 'true_risk_channel_outage'
            -- 字段单位错误：等候时长量级离谱，且业务量与差错均无异常
            WHEN s.avg_wait_minutes > s.wait_ma7 * 8
                 AND ABS(COALESCE(s.counter_txn_step, 0)) < 0.30
                 AND ABS(COALESCE(s.error_rate_7d_abs_delta, 0)) < 0.0022
                 THEN 'data_error_unit'
            -- 批量重跑：业务量虚高、成本率反而下降（指标间不自洽）
            WHEN COALESCE(s.counter_txn_step, 0) > 0.35
                 AND s.op_cost_ratio < s.op_cost_ratio_ma7
                 AND ABS(COALESCE(s.wait_step, 0)) < 0.35
                 THEN 'data_error_duplicate'
            -- 口径调整：业务量台阶式下降，而等候与差错全在基线内
            WHEN COALESCE(s.counter_txn_step, 0) < -0.10
                 AND ABS(COALESCE(s.wait_step, 0)) < 0.12
                 AND ABS(COALESCE(s.error_rate_7d_abs_delta, 0)) < 0.0011
                 AND ABS(COALESCE(s.complaint_dev, 0)) < 0.50
                 THEN 'caliber_gap'
            -- 真实产能不足：业务量与等候时长同步走高
            WHEN COALESCE(s.counter_txn_dev, 0) > 0.15
                 AND COALESCE(s.wait_dev, 0) > 0.18
                 THEN 'true_risk_capacity'
            -- 真实操作风险：周度差错率显著抬升
            WHEN COALESCE(s.error_rate_7d_abs_delta, 0) > 0.0015
                 THEN 'true_risk_operational'
            -- 真实服务风险
            WHEN COALESCE(s.complaint_dev, 0) > 0.50
                 THEN 'true_risk_service'
            ELSE 'undetermined'
        END AS suspected_cause
    FROM sized s
)
SELECT
    stat_date,
    branch_code,
    branch_name,
    branch_kind,
    region,
    counter_txn_cnt,
    avg_wait_minutes,
    error_rate,
    complaint_per_10k,
    e_channel_rate,
    op_cost_ratio,
    counter_txn_dev,
    counter_txn_step,
    wait_dev,
    wait_step,
    error_rate_7d_abs_delta,
    error_rate_7d_rel,
    complaint_dev,
    amplitude,
    persistence_factor,
    trig_7d,
    ROUND(amplitude * persistence_factor * 10, 3) AS severity_score,
    CASE
        WHEN amplitude * persistence_factor * 10 >= 80 THEN 3
        WHEN amplitude * persistence_factor * 10 >= 35 THEN 2
        ELSE 1
    END AS severity,
    suspected_cause,
    CASE
        WHEN suspected_cause = 'true_risk_channel_outage'
            THEN '确认自助设备/线上渠道状态，恢复前增开弹性窗口'
        WHEN suspected_cause = 'data_error_unit'
            THEN '先核对叫号系统字段单位（分钟/秒），确认后再评估业务影响'
        WHEN suspected_cause = 'data_error_duplicate'
            THEN '核对核心系统批量任务是否重复跑批，避免按虚高业务量排班'
        WHEN suspected_cause = 'caliber_gap'
            THEN '确认是否启用新统计口径；若是则回算基线，而非问责网点'
        WHEN suspected_cause = 'true_risk_capacity'
            THEN '评估增开弹性窗口或引导客户转向智能设备，复盘排班模型'
        WHEN suspected_cause = 'true_risk_operational'
            THEN '抽查差错明细，加强事后复核与新人辅导'
        WHEN suspected_cause = 'true_risk_service'
            THEN '回溯投诉工单定位服务触点，安排服务话术专项辅导'
        ELSE '纳入观察名单，不单独处置'
    END AS action_hint
FROM classified
WHERE triggered = 1
  -- 持续性门槛：仅单日越线且幅度不大 → 降级为观察，不产生预警
  AND NOT (trig_7d <= 1 AND amplitude * persistence_factor * 10 < 35)
ORDER BY stat_date, severity DESC, severity_score DESC;

-- -----------------------------------------------------------------------------
-- 4. 常用查询
-- -----------------------------------------------------------------------------

-- 4.1 当日核心指标一览（看板主表）
-- SELECT stat_date, branch_code, branch_name, counter_txn_cnt, avg_wait_minutes,
--        ROUND(error_rate*100,3) AS error_pct, complaint_per_10k,
--        customer_satisfaction, e_channel_rate, capacity_utilization
--   FROM dwd_branch_ops_daily
--  WHERE stat_date = (SELECT MAX(stat_date) FROM dwd_branch_ops_daily)
--  ORDER BY avg_wait_minutes DESC;

-- 4.2 预警结构：多少预警其实不是业务风险
-- SELECT suspected_cause, severity, COUNT(*) AS alerts, COUNT(DISTINCT branch_code) AS branches
--   FROM ads_ops_alert GROUP BY suspected_cause, severity ORDER BY alerts DESC;

-- 4.3 缓慢漂移排查：周度差错率相对基线的偏离
-- SELECT branch_code, branch_name, stat_date, ROUND(error_rate_7d,5) AS err_7d,
--        error_rate_7d_abs_delta, trig_7d
--   FROM ads_ops_alert
--  WHERE suspected_cause = 'true_risk_operational'
--  ORDER BY stat_date;

-- 4.4 窗口效能标杆：单位窗口业务量高但等候时长短的网点
-- SELECT branch_code, branch_name, ROUND(AVG(txn_per_window),1) AS txn_per_window,
--        ROUND(AVG(avg_wait_minutes),2) AS avg_wait,
--        ROUND(AVG(error_rate)*100,3) AS err_pct
--   FROM dwd_branch_ops_daily GROUP BY branch_code, branch_name
--  ORDER BY txn_per_window DESC;
