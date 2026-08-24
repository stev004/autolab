# V0 simulated assay

The simulator accepts executed concentration `c`, reagent volume `Vr`, sample volume
`Vs`, incubation time `t`, and temperature `T`. Its private response surface is not
reachable from an optimiser.

Reagent amount per well is:

```text
m = c × Vr / 1000  [mg]
```

The sensitivity combines reagent saturation, a preferred sample:reagent ratio, a
Gaussian temperature optimum, saturating incubation kinetics, and concentration/
volume interactions:

```text
reagent_saturation = 1 - exp(-m / 0.038)
ratio_efficiency   = 0.54 + 0.46 exp(-0.5 ((Vs/Vr - 0.58) / 0.48)^2)
temperature_eff    = 0.38 + 0.62 exp(-0.5 ((T - 29.5) / 6.2)^2)
tau                = 8.2 / max(0.45, temperature_eff)
kinetic_completion = 1 - exp(-t / tau)
```

Additional bounded interactions multiply these terms. Expected absorbance at analyte
level `a` is nonlinear:

```text
linear_signal = sensitivity × a
expected = blank_offset
         + linear_signal / (1 + 0.17 × linear_signal)
         + 0.012 × sensitivity × a^2
```

Each observed well adds seeded batch drift, position-dependent drift, well effect,
heteroscedastic Gaussian noise, and an occasional signed outlier before detector
clipping. Low volumes, poor temperature and poor ratios increase relative noise.
Failure injection can create a missing well, outlier, contaminated blank, excessive
drift, sensor timeout, incubation failure, or ambiguous dispense.

Cost per test is fixed consumables plus reagent amount, sample volume and incubation
time costs. Batch totals use the actually executed well count.

## Reproducibility

Independent NumPy streams are derived from campaign seed, simulator seed, experiment
identity/ordinal, well identity and noise-component name. Adding one noise component
does not shift unrelated streams. Every batch records model ID/version, simulator
configuration hash, seeds and stream namespace.

Fixed seeds reproduce software behavior; they do not establish physical
reproducibility or validate the scientific realism of the hidden surface.
