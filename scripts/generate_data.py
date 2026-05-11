#!/usr/bin/env python3
"""
generate_data.py  –  Run once after cloning to seed the data/ directory.

Usage (from repo root):
    python3 scripts/generate_data.py

Creates:
    data/questions_bank.json    – 200 pilot statistics Q&A records
    data/sample_submissions.csv – 30 synthetic gold-marked student submissions
"""
import json, csv, random, os

random.seed(42)
ROOT     = os.path.join(os.path.dirname(__file__), "..")
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DIFF   = ["easy", "medium", "hard"]
QTYPES = ["short_answer", "mcq", "true_false"]

BASE = [
  ("What is the difference between mean and median, and when would you prefer one over the other?",
   "The mean is the arithmetic average of all values; the median is the middle value when data is sorted. "
   "Prefer median for skewed data or when outliers are present; prefer mean for symmetric, outlier-free distributions.",
   "2 marks: correct definitions. 2 marks: conditions for each measure. 1 mark: example.", 5, "Descriptive Statistics"),
  ("Define variance and standard deviation and explain how they are related.",
   "Variance is the average of squared deviations from the mean. Standard deviation is the square root of variance "
   "and is expressed in the same units as the original data.",
   "2 marks: variance. 2 marks: SD. 1 mark: units / interpretability.", 5, "Descriptive Statistics"),
  ("Explain the interquartile range (IQR) and Tukey's outlier fences.",
   "IQR = Q3 - Q1 (middle 50% of data). Outliers: below Q1 - 1.5*IQR or above Q3 + 1.5*IQR.",
   "2 marks: IQR formula and interpretation. 3 marks: outlier fences.", 5, "Descriptive Statistics"),
  ("Name and describe the five components of a box plot.",
   "Minimum, Q1, median (Q2), Q3, maximum. Whiskers reach the last non-outlier; outliers plotted individually.",
   "1 mark per component (5 total).", 5, "Descriptive Statistics"),
  ("Distinguish between population parameters and sample statistics.",
   "Parameters (mu, sigma) describe the entire population and are usually unknown. "
   "Statistics (x-bar, s) are computed from samples and used to estimate parameters.",
   "2 marks: parameter. 2 marks: statistic. 1 mark: sampling variability.", 5, "Descriptive Statistics"),
  ("State Bayes' theorem and define each component.",
   "P(A|B) = P(B|A)*P(A)/P(B). Posterior=P(A|B), likelihood=P(B|A), prior=P(A), marginal=P(B).",
   "1 mark: formula. 1 mark each: posterior, likelihood, prior, marginal.", 5, "Probability"),
  ("Distinguish mutually exclusive from independent events.",
   "Mutually exclusive: P(A and B)=0. Independent: P(A and B)=P(A)*P(B). "
   "Non-zero ME events cannot be independent.",
   "2 marks: ME. 2 marks: independence. 1 mark: relationship.", 5, "Probability"),
  ("State the four conditions required for a Binomial distribution.",
   "Fixed n, binary outcomes, constant p, independent trials.",
   "1 mark per condition x4, 1 mark: parameters n and p.", 5, "Probability"),
  ("Explain the law of total probability.",
   "If B1..Bn partition the sample space: P(A) = sum P(A|Bi)*P(Bi).",
   "2 marks: formula. 2 marks: partition requirement. 1 mark: example.", 5, "Probability"),
  ("Explain Type I and Type II errors in hypothesis testing.",
   "Type I (alpha): rejecting a true H0 (false positive). Type II (beta): failing to reject false H0 (false negative). Reducing alpha increases beta.",
   "2 marks: Type I. 2 marks: Type II. 1 mark: trade-off.", 5, "Hypothesis Testing"),
  ("Define the p-value and explain the decision rule.",
   "P-value = probability of data as extreme as observed given H0 is true. Reject H0 if p < alpha. NOT the probability H0 is true.",
   "2 marks: definition. 1 mark: rule. 1 mark: misconception. 1 mark: alpha link.", 5, "Hypothesis Testing"),
  ("What is statistical power and what factors increase it?",
   "Power = 1-beta. Increases with: larger n, larger effect size, higher alpha, lower variability.",
   "1 mark: definition. 1 mark each: four factors.", 5, "Hypothesis Testing"),
  ("What assumptions underlie an independent-samples t-test?",
   "1) Independence. 2) Normality or large n (CLT). 3) Homogeneity of variance. 4) Continuous data.",
   "1 mark per assumption x4, 1 mark: Levene's test.", 5, "Hypothesis Testing"),
  ("What does R-squared represent in linear regression?",
   "Proportion of variance in Y explained by the model. Ranges 0-1. High R2 does not guarantee good model.",
   "2 marks: definition. 1 mark: range. 1 mark: example. 1 mark: limitation.", 5, "Regression Analysis"),
  ("State the four OLS regression assumptions.",
   "1) Linearity. 2) Independence of residuals. 3) Homoscedasticity. 4) Normality of residuals.",
   "1 mark per assumption x4, 1 mark: Gauss-Markov.", 5, "Regression Analysis"),
  ("What is multicollinearity and why is it a problem?",
   "High predictor correlation inflates SEs, making coefficients unstable. Detected via VIF > 10.",
   "2 marks: definition. 2 marks: consequences. 1 mark: VIF.", 5, "Regression Analysis"),
  ("Interpret a 95% confidence interval for a population mean.",
   "95% of such intervals from repeated samples would contain the true mean. "
   "This specific interval either contains or does not contain the true value.",
   "2 marks: correct interpretation. 2 marks: misconception. 1 mark: clarity.", 5, "Confidence Intervals"),
  ("How does increasing sample size affect CI width?",
   "Larger n reduces SE=sigma/sqrt(n), narrowing the CI. Quadrupling n halves the width.",
   "2 marks: SE formula. 2 marks: direction. 1 mark: quantitative example.", 5, "Confidence Intervals"),
  ("What is the margin of error and how is it calculated?",
   "MOE = z* x sigma/sqrt(n). Maximum expected difference between estimate and true parameter.",
   "2 marks: formula. 2 marks: interpretation. 1 mark: z* role.", 5, "Confidence Intervals"),
  ("State the empirical 68-95-99.7 rule for a normal distribution.",
   "~68% within 1 SD, ~95% within 2 SD, ~99.7% within 3 SD of the mean.",
   "1 mark per rule x3. 1 mark: normality. 1 mark: practical use.", 5, "Normal Distribution"),
  ("Define a z-score and explain its use.",
   "z = (x-mu)/sigma. Measures SDs from the mean. Allows comparison across distributions.",
   "2 marks: formula. 2 marks: interpretation. 1 mark: use case.", 5, "Normal Distribution"),
  ("Explain the Central Limit Theorem and its importance.",
   "Sampling distribution of x-bar approaches normality as n increases regardless of population shape (n>=30). "
   "Justifies normal-based inference for large samples.",
   "2 marks: statement. 2 marks: conditions. 1 mark: importance.", 5, "Normal Distribution"),
  ("When is a chi-square test of independence appropriate?",
   "Testing association between two categorical variables in a contingency table. Expected cell frequencies >= 5.",
   "2 marks: purpose. 2 marks: assumptions. 1 mark: H0.", 5, "Chi-Square Tests"),
  ("How is expected frequency calculated in a chi-square test?",
   "E = (row total x column total) / grand total. Represents counts expected under independence.",
   "2 marks: formula. 2 marks: interpretation. 1 mark: H0.", 5, "Chi-Square Tests"),
  ("What is the purpose of one-way ANOVA?",
   "Tests equality of means across 3+ groups. H0: all means equal. Partitions variance into between and within groups.",
   "2 marks: purpose. 1 mark: H0. 2 marks: variance partitioning.", 5, "ANOVA"),
  ("What does a significant ANOVA F-test tell you?",
   "At least one group mean differs. Does NOT identify which pair. Post-hoc tests (Tukey, Bonferroni) needed.",
   "2 marks: what it shows. 2 marks: limitation. 1 mark: post-hoc.", 5, "ANOVA"),
  ("Compare simple random sampling and stratified sampling.",
   "SRS: equal probability for all units. Stratified: random sample from each stratum, ensuring subgroup representation.",
   "2 marks: SRS. 2 marks: stratified. 1 mark: when to prefer stratified.", 5, "Sampling Methods"),
  ("What is sampling bias and how is it minimised?",
   "Systematic error from non-representative sample. Minimised by random sampling, defined frame, adequate n.",
   "2 marks: definition. 3 marks: prevention methods.", 5, "Sampling Methods"),
  ("Compare frequentist and Bayesian statistical approaches.",
   "Frequentist: probability=long-run frequency, fixed parameters. Bayesian: probability=belief, "
   "posterior = prior x likelihood.",
   "2 marks: frequentist. 2 marks: Bayesian. 1 mark: key difference.", 5, "Bayesian Statistics"),
  ("What is heteroscedasticity and how can it be addressed?",
   "Non-constant residual variance. Detected via residual plots or Breusch-Pagan. "
   "Fix: log-transform Y, robust SEs, or WLS.",
   "2 marks: definition. 1 mark: detection. 2 marks: remedies.", 5, "Regression Analysis"),
]

questions = []
for i in range(200):
    src = BASE[i % len(BASE)]
    suffix = "" if i < len(BASE) else f" [Variant {i - len(BASE) + 2}]"
    questions.append({
        "id": i + 1,
        "question_text": src[0] + suffix,
        "question_type": QTYPES[i % 3],
        "model_answer": src[1],
        "rubric": src[2],
        "max_marks": src[3],
        "topic_tag": src[4],
        "difficulty": DIFF[i % 3],
        "worked_solution": src[1],
    })

qpath = os.path.join(DATA_DIR, "questions_bank.json")
with open(qpath, "w") as f:
    json.dump(questions, f, indent=2)
print(f"Written {len(questions)} questions -> {qpath}")

# ─── Sample Submissions (30 gold-marked) ─────────────────────────────────────
ANSWERS = [
    "The mean sums all values divided by n; the median is the middle value. Use median for skewed data.",
    "Variance is average squared deviation; SD is its square root in original units.",
    "IQR = Q3 - Q1. Outliers fall below Q1-1.5*IQR or above Q3+1.5*IQR.",
    "Type I rejects a true null (false positive). Type II fails to reject a false null (false negative).",
    "P-value = P(data this extreme | H0). Reject H0 when p < alpha.",
    "R-squared is the proportion of variance in Y explained by the regression model.",
    "Normal distribution is symmetric bell-shaped; about 95% of values lie within 2 SDs.",
    "CLT: sampling distribution of x-bar approaches normal as n increases.",
    "A 95% CI means 95% of such intervals from repeated samples contain the true parameter.",
    "Power is the probability of correctly rejecting a false null hypothesis.",
]
SIDS = [f"S{str(i).zfill(3)}" for i in range(1, 31)]
rows = [
    {
        "student_id": sid,
        "question_id": questions[i % 10]["id"],
        "question_text": questions[i % 10]["question_text"],
        "student_answer": ANSWERS[i % len(ANSWERS)],
        "gold_mark": round(random.uniform(2.0, 5.0), 1),
        "max_mark": questions[i % 10]["max_marks"],
        "gold_feedback": "Demonstrates core understanding. Score reflects rubric compliance.",
    }
    for i, sid in enumerate(SIDS)
]

spath = os.path.join(DATA_DIR, "sample_submissions.csv")
with open(spath, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)
print(f"Written {len(rows)} submissions -> {spath}")
print("\nDone. Import questions via the instructor UI or POST to /api/v1/questions/.")
