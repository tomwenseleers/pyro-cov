import datetime
import functools
import gc
import logging
import math
import pickle
import re
from collections import Counter, OrderedDict, defaultdict
from timeit import default_timer

import pyro
import pyro.distributions as dist
import torch
from pyro import poutine
from pyro.infer import SVI, JitTrace_ELBO, Trace_ELBO
from pyro.infer.autoguide import (
    AutoDelta,
    AutoGuideList,
    AutoLowRankMultivariateNormal,
    AutoNormal,
)
from pyro.infer.reparam import LocScaleReparam
from pyro.ops.streaming import CountMeanVarianceStats, StatsOfDict
from pyro.ops.welford import WelfordCovariance
from pyro.optim import ClippedAdam
from pyro.poutine.util import site_is_subsample

import pyrocov.geo
from pyrocov import pangolin
from pyrocov.util import pearson_correlation

logger = logging.getLogger(__name__)

# Reasonable values might be week (7), fortnight (14), or month (28)
TIMESTEP = 14  # in days
GENERATION_TIME = 5.5  # in days
START_DATE = "2019-12-01"


def get_fine_regions(columns, min_samples=50):
    """
    Select regions that have at least ``min_samples`` samples.
    Remaining regions will be coarsely aggregated into country level.
    """
    # Count number of samples in each subregion.
    counts = Counter()
    for location in columns["location"]:
        parts = location.split("/")
        if len(parts) < 2:
            continue
        parts = tuple(p.strip() for p in parts[:3])
        counts[parts] += 1

    # Select fine countries.
    return frozenset(parts for parts, count in counts.items() if count >= min_samples)


def load_gisaid_data(
    *,
    device="cpu",
    include={},
    exclude={},
):
    logger.info("Loading data")
    include = {k: re.compile(v) for k, v in include.items()}
    exclude = {k: re.compile(v) for k, v in exclude.items()}
    with open("results/gisaid.columns.pkl", "rb") as f:
        columns = pickle.load(f)
    logger.info("Training on {} rows with columns:".format(len(columns["day"])))
    logger.info(", ".join(columns.keys()))
    fine_regions = get_fine_regions(columns)

    # Filter features into numbers of mutations.
    aa_features = torch.load("results/nextclade.features.pt")
    mutations = aa_features["mutations"]
    features = aa_features["features"].to(
        device=device, dtype=torch.get_default_dtype()
    )
    keep = [m.count(",") == 0 for m in mutations]
    mutations = [m for k, m in zip(keep, mutations) if k]
    features = features[:, keep]
    logger.info("Loaded {} feature matrix".format(" x ".join(map(str, features.shape))))

    # Aggregate regions.
    lineages = list(map(pangolin.compress, columns["lineage"]))
    lineage_id_inv = list(map(pangolin.compress, aa_features["lineages"]))
    lineage_id = {k: i for i, k in enumerate(lineage_id_inv)}
    sparse_data = Counter()
    location_id = OrderedDict()
    skipped = set()
    for virus_name, day, location, lineage in zip(
        columns["virus_name"], columns["day"], columns["location"], lineages
    ):
        row = {"virus_name": virus_name, "location": location}
        if lineage not in lineage_id:
            if lineage not in skipped:
                skipped.add(lineage)
                logger.warning(f"WARNING skipping unsampled lineage {lineage}")
            continue
        if not all(v.search(row[k]) for k, v in include.items()):
            continue
        if any(v.search(row[k]) for k, v in exclude.items()):
            continue
        parts = location.split("/")
        if len(parts) < 2:
            continue
        parts = tuple(p.strip() for p in parts[:3])
        if len(parts) == 3 and parts not in fine_regions:
            parts = parts[:2]
        location = " / ".join(parts)
        p = location_id.setdefault(location, len(location_id))
        s = lineage_id[lineage]
        t = day // TIMESTEP
        sparse_data[t, p, s] += 1

    T = 1 + max(columns["day"]) // TIMESTEP
    P = len(location_id)
    S = len(lineage_id)
    weekly_strains = torch.zeros(T, P, S)
    for (t, p, s), n in sparse_data.items():
        weekly_strains[t, p, s] = n
    logger.info(
        f"Keeping {int(weekly_strains.sum())}/{len(lineages)} rows "
        f"(dropped {len(lineages) - int(weekly_strains.sum())})"
    )

    # Filter regions.
    num_times_observed = (weekly_strains > 0).max(2).values.sum(0)
    ok_regions = (num_times_observed >= 2).nonzero(as_tuple=True)[0]
    ok_region_set = set(ok_regions.tolist())
    logger.info(f"Keeping {len(ok_regions)}/{weekly_strains.size(1)} regions")
    weekly_strains = weekly_strains.index_select(1, ok_regions)
    locations = [k for k, v in location_id.items() if v in ok_region_set]
    location_id = OrderedDict(zip(locations, range(len(locations))))

    # Construct region-local time scales centered around observations.
    num_obs = weekly_strains.sum(-1)
    local_time = torch.arange(float(len(num_obs))) * TIMESTEP / GENERATION_TIME
    local_time = local_time[:, None]
    local_time = local_time - (local_time * num_obs).sum(0) / num_obs.sum(0)

    return {
        "location_id": location_id,
        "mutations": mutations,
        "weekly_strains": weekly_strains,
        "features": features,
        "lineage_id": lineage_id,
        "lineage_id_inv": lineage_id_inv,
        "local_time": local_time,
    }


def subset_gisaid_data(
    gisaid_dataset,
    location_queries=None,
    max_strains=math.inf,
):
    old = gisaid_dataset
    new = old.copy()

    # Select locations.
    if location_queries is not None:
        locations = sorted(
            {
                location
                for location in new["location_id"]
                if any(q in location for q in location_queries)
            }
        )
        ids = torch.tensor([old["location_id"][location] for location in locations])
        new["location_id"] = {name: i for i, name in enumerate(locations)}
        new["weekly_strains"] = new["weekly_strains"].index_select(1, ids)
        new["local_time"] = new["local_time"].index_select(1, ids)

    # Select strains.
    if new["weekly_strains"].size(-1) > max_strains:
        ids = (
            new["weekly_strains"]
            .sum([0, 1])
            .sort(0, descending=True)
            .indices[:max_strains]
        )
        new["weekly_strains"] = new["weekly_strains"].index_select(-1, ids)
        new["features"] = new["features"].index_select(0, ids)
        new["lineage_id_inv"] = [new["lineage_id_inv"][i] for i in ids.tolist()]
        new["lineage_id"] = {name: i for i, name in enumerate(new["lineage_id_inv"])}

    # Select mutations.
    gaps = new["features"].max(0).values - new["features"].min(0).values
    ids = (gaps >= 0.5).nonzero(as_tuple=True)[0]
    new["mutations"] = [new["mutations"][i] for i in ids.tolist()]
    new["features"] = new["features"].index_select(-1, ids)

    logger.info(
        "Selected {}/{} places, {}/{} strains, {}/{} mutations, {}/{} samples".format(
            len(new["location_id"]),
            len(old["location_id"]),
            len(new["lineage_id"]),
            len(old["lineage_id"]),
            len(new["mutations"]),
            len(old["mutations"]),
            int(new["weekly_strains"].sum()),
            int(old["weekly_strains"].sum()),
        )
    )

    return new


def load_jhu_data(gisaid_data):
    # Load raw JHU case count data.
    us_cases_df = pyrocov.geo.read_csv("time_series_covid19_confirmed_US.csv")
    global_cases_df = pyrocov.geo.read_csv("time_series_covid19_confirmed_global.csv")
    daily_cases = torch.cat(
        [
            pyrocov.geo.pd_to_torch(us_cases_df, columns=slice(11, None)),
            pyrocov.geo.pd_to_torch(global_cases_df, columns=slice(4, None)),
        ]
    ).T
    logger.info(
        "Loaded {} x {} daily case data, totaling {}".format(
            *daily_cases.shape, daily_cases[-1].sum().item()
        )
    )

    # Convert JHU locations to GISAID locations.
    locations = list(gisaid_data["location_id"])
    matrix = pyrocov.geo.gisaid_to_jhu_location(locations, us_cases_df, global_cases_df)
    assert matrix.shape == (len(locations), daily_cases.shape[-1])
    daily_cases = daily_cases @ matrix.T
    daily_cases[1:] -= daily_cases[:-1].clone()  # cumulative -> density
    daily_cases.clamp_(min=0)
    assert daily_cases.shape[1] == len(gisaid_data["location_id"])

    # Convert daily counts to TIMESTEP counts (e.g. weekly).
    start_date = datetime.datetime.strptime(START_DATE, "%Y-%m-%d")
    jhu_start_date = pyrocov.geo.parse_date(us_cases_df.columns[11])
    assert start_date < jhu_start_date
    dt = (jhu_start_date - start_date).days
    T = len(gisaid_data["weekly_strains"])
    weekly_cases = daily_cases.new_zeros(T, len(locations))
    for w in range(TIMESTEP):
        t0 = (w + dt) // TIMESTEP
        source = daily_cases[w::TIMESTEP]
        destin = weekly_cases[t0 : t0 + len(source)]
        destin[:] += source[: len(destin)]
    assert weekly_cases.sum() > 0

    return {
        "daily_cases": daily_cases.clamp(min=0),
        "weekly_cases": weekly_cases.clamp(min=0),
    }


def model(dataset, *, model_type=""):
    weekly_strains = dataset["weekly_strains"]
    features = dataset["features"]
    local_time = dataset["local_time"][..., None]
    if not torch._C._get_tracing_state():
        assert weekly_strains.shape[-1] == features.shape[0]
        assert dataset["local_time"].shape == weekly_strains.shape[:2]
    T, P, S = weekly_strains.shape
    S, F = features.shape
    place_plate = pyro.plate("place", P, dim=-1)
    time_plate = pyro.plate("time", T, dim=-2)

    # Configure reparametrization (which does not affect model density).
    reparam = {}
    if "reparam" in model_type:
        local_time = local_time + pyro.param("local_time", lambda: torch.zeros(P, S))
        reparam["rate_coef"] = LocScaleReparam()
        if "biased" in model_type:
            reparam["rate"] = LocScaleReparam()
        if "overdispersed" in model_type:
            reparam["logits"] = LocScaleReparam()
    with poutine.reparam(config=reparam):
        # Sample global random variables.
        feature_scale = pyro.sample("feature_scale", dist.LogNormal(math.log(0.1), 1))
        if "overdispersed" in model_type or "dirichlet" in model_type:
            logits_scale = pyro.sample("logits_scale", dist.Uniform(1e-3, 1e-1))[
                ..., None
            ]

        # Assume relative growth rate depends strongly on mutations and weakly on place.
        rate_coef = pyro.sample(
            "rate_coef",
            dist.SoftLaplace(torch.zeros(F), feature_scale[..., None]).to_event(1),
        )
        rate_loc = 0.01 * rate_coef @ features.T
        if "biased" in model_type:
            if "locally" in model_type:
                strain_scale = pyro.sample(
                    "strain_scale", dist.LogNormal(-4 * torch.ones(S), 2).to_event(1)
                )
            else:
                rate_scale = pyro.sample("rate_scale", dist.LogNormal(-4, 2))[..., None]
        with place_plate:
            if "biased" in model_type:
                if "locally" in model_type:
                    place_scale = pyro.sample("place_scale", dist.LogNormal(-4, 2))
                    rate_scale = (
                        place_scale[..., None] ** 2 + strain_scale ** 2
                    ).sqrt()
                rate = pyro.sample(
                    "rate", dist.Normal(rate_loc, rate_scale).to_event(1)
                )
            else:
                rate = pyro.deterministic("rate", rate_loc, event_dim=1)

            # Assume initial infections depend on place.
            init = pyro.sample("init", dist.SoftLaplace(torch.zeros(S), 10).to_event(1))

            # Model overdispersion.
            with time_plate:
                logits_loc = init + rate * local_time
                if not torch._C._get_tracing_state():  # Save during prediction.
                    pyro.deterministic("probs", logits_loc.detach().softmax(-1))
                if "overdispersed" in model_type:
                    logits = pyro.sample(
                        "logits", dist.Normal(logits_loc, logits_scale).to_event(1)
                    )
                elif "dirichlet" in model_type:
                    concentration = logits_loc.softmax(-1) / logits_scale
                    concentration.data.add_(1e-20)
                else:
                    logits = pyro.deterministic("logits", logits_loc, event_dim=1)

                # Finally observe counts.
                if "dirichlet" in model_type:
                    pyro.sample(
                        "obs",
                        dist.DirichletMultinomial(
                            concentration=concentration,
                            is_sparse=True,
                            validate_args=False,
                        ),
                        obs=weekly_strains,
                    )
                else:
                    pyro.sample(
                        "obs",
                        dist.Multinomial(logits=logits, validate_args=False),
                        obs=weekly_strains,
                    )


class InitLocFn:
    def __init__(self, dataset, init_data={}):
        self.__dict__.update(init_data)

        # Initialize init.
        if "init" not in init_data:
            init = dataset["weekly_strains"].sum(0)
            init.add_(1 / init.size(-1)).div_(init.sum(-1, True))
            init.log_().sub_(init.median(-1, True).values)
            self.init = init
        assert not torch.isnan(self.init).any()
        logger.info(f"init stddev = {self.init.std():0.3g}")

        # Initialize logits.
        if "logits" in init_data:
            pass
        elif "rate_coef" in init_data and "init" in init_data:
            self.rate = 0.01 * (self.rate_coef @ dataset["features"].T)
            self.logits = self.init + self.rate * dataset["local_time"][:, :, None]
        else:
            weekly_strains = dataset["weekly_strains"]
            self.logits = weekly_strains.add(1 / weekly_strains.size(-1)).log()
            self.logits -= self.logits.mean(-1, True)
            self.logits += torch.rand(self.logits.shape).sub_(0.5).mul_(0.001)
            self.logits_decentered = self.logits

    def __call__(self, site):
        name = site["name"]
        shape = site["fn"].shape()
        if hasattr(self, name):
            result = getattr(self, name)
            assert result.shape == shape
            return result
        if name == "feature_scale":
            return torch.ones(shape)
        if name == "logits_scale":
            return torch.full(shape, 0.002)
        if name in ("rate_scale", "place_scale", "strain_scale"):
            return torch.full(shape, 0.01)
        if name in ("rate_coef", "rate_coef_decentered", "rate", "rate_decentered"):
            return torch.rand(shape).sub_(0.5).mul_(0.01)
        raise ValueError(f"InitLocFn found unhandled site {repr(name)}; please update.")


def site_is_global(site):
    return (
        site["type"] == "sample"
        and hasattr(site["fn"], "shape")
        and site["fn"].shape().numel() == 1
    )


class Guide(AutoGuideList):
    def __init__(self, model, init_loc_fn, init_scale):
        super().__init__(model)

        # Jointly estimate mutation coefficients and shrinkage.
        mvn = ["feature_scale", "rate_coef", "rate_coef_decentered"]
        self.append(
            AutoLowRankMultivariateNormal(
                poutine.block(model, expose=mvn),
                init_loc_fn=init_loc_fn,
                init_scale=init_scale,
                rank=200,
            )
        )
        model = poutine.block(model, hide=mvn)

        # Mean-field estimate all remaining sites.
        self.append(AutoNormal(model, init_loc_fn=init_loc_fn, init_scale=init_scale))


@torch.no_grad()
@poutine.mask(mask=False)
def predict(
    guide,
    dataset,
    *,
    num_samples=1000,
    vectorize=None,
    save_params=("rate", "probs"),
):
    model = guide.model

    def get_conditionals(data):
        trace = poutine.trace(poutine.condition(model, data)).get_trace(dataset)
        return {
            name: site["value"].detach()
            for name, site in trace.nodes.items()
            if site["type"] == "sample" and not site_is_subsample(site)
            if name != "obs"
        }

    # Compute median point estimate.
    result = defaultdict(dict)
    for name, value in get_conditionals(guide.median(dataset)).items():
        if value.numel() < 1e5 or name in save_params:
            result["median"][name] = value

    # Compute moments.
    save_params = {
        k for k, v in result["median"].items() if v.numel() < 1e5 or k in save_params
    }
    if vectorize is None:
        vectorize = result["median"]["probs"].numel() < 1e5
    if vectorize:
        with pyro.plate("particles", num_samples, dim=-3):
            samples = get_conditionals(guide())
        for k, v in samples.items():
            if k in save_params:
                result["mean"][k] = v.mean(0).squeeze()
                result["std"][k] = v.std(0).squeeze()
    else:
        stats = StatsOfDict({k: CountMeanVarianceStats for k in save_params})
        for _ in range(num_samples):
            stats.update(get_conditionals(guide()))
            print(".", end="", flush=True)
        for name, stats_ in stats.get().items():
            if "mean" in stats_:
                result["mean"][name] = stats_["mean"]
            if "variance" in stats_:
                result["std"][name] = stats_["variance"].sqrt()
    return dict(result)


def fit_svi(
    dataset,
    *,
    model_type,
    guide_type,
    init_data={},
    cond_data={},
    learning_rate=0.05,
    learning_rate_decay=0.1,
    num_steps=3001,
    num_samples=1000,
    clip_norm=10.0,
    rank=10,
    jit=True,
    log_every=50,
    seed=20210319,
    check_loss=False,
):
    start_time = default_timer()

    if isinstance(init_data, str):
        init_data = fit_svi(
            dataset,
            model_type=init_data,
            cond_data=cond_data,
            guide_type="map",
            learning_rate=0.05,
            learning_rate_decay=1.0,
            num_steps=1001,
            log_every=log_every,
            seed=seed,
        )["median"]

    logger.info(f"Fitting {guide_type} guide via SVI")
    pyro.set_rng_seed(seed)
    pyro.clear_param_store()
    param_store = pyro.get_param_store()

    # Initialize guide so we can count parameters and register hooks.
    cond_data = {k: torch.as_tensor(v) for k, v in cond_data.items()}
    model_ = poutine.condition(model, cond_data)
    model_ = functools.partial(model_, model_type=model_type)
    init_loc_fn = InitLocFn(dataset, init_data)
    if guide_type == "map":
        guide = AutoDelta(model_, init_loc_fn=init_loc_fn)
    elif guide_type == "normal":
        guide = AutoNormal(model_, init_loc_fn=init_loc_fn, init_scale=0.01)
    elif guide_type == "full":
        guide = AutoLowRankMultivariateNormal(
            model_, init_loc_fn=init_loc_fn, init_scale=0.01
        )
    else:
        guide = Guide(model_, init_loc_fn=init_loc_fn, init_scale=0.01)
    # This initializes the guide:
    latent_shapes = {k: v.shape for k, v in guide(dataset).items()}
    latent_numel = {k: v.numel() for k, v in latent_shapes.items()}
    logger.info(
        "\n".join(
            [f"Model has {sum(latent_numel.values())} latent variables of shapes:"]
            + [f" {k} {tuple(v)}" for k, v in latent_shapes.items()]
        )
    )
    param_shapes = {k: v.shape for k, v in pyro.get_param_store().named_parameters()}
    param_numel = {k: v.numel() for k, v in param_shapes.items()}
    logger.info(
        "\n".join(
            [f"Guide has {sum(param_numel.values())} parameters of shapes:"]
            + [f" {k} {tuple(v)}" for k, v in param_shapes.items()]
        )
    )

    # Log gradient norms during inference.
    series = defaultdict(list)

    def hook(g, series):
        series.append(torch.linalg.norm(g.reshape(-1), math.inf).item())

    for name, value in pyro.get_param_store().named_parameters():
        value.register_hook(functools.partial(hook, series=series[name]))

    def optim_config(param_name):
        config = {
            "lr": learning_rate,
            "lrd": learning_rate_decay ** (1 / num_steps),
            "clip_norm": clip_norm,
        }
        scalars = [k for k, v in latent_numel.items() if v == 1]
        if any("locs." + s in name for s in scalars):
            config["lr"] *= 0.2
        elif "scales" in param_name:
            config["lr"] *= 0.1
        elif "scale_tril" in param_name:
            config["lr"] *= 0.05
        elif "weight" in param_name:
            config["lr"] *= 0.05
        elif "_centered" in param_name:
            config["lr"] *= 0.1
        return config

    optim = ClippedAdam(optim_config)
    Elbo = JitTrace_ELBO if jit else Trace_ELBO
    elbo = Elbo(max_plate_nesting=2, ignore_jit_warnings=True)
    svi = SVI(model_, guide, optim, elbo)
    losses = []
    num_obs = dataset["weekly_strains"].count_nonzero()
    for step in range(num_steps):
        loss = svi.step(dataset=dataset)
        assert not math.isnan(loss)
        losses.append(loss)
        median = guide.median()
        for name, value in median.items():
            if value.numel() == 1:
                series[name].append(float(value))
        if log_every and step % log_every == 0:
            logger.info(
                " ".join(
                    [f"step {step: >4d} L={loss / num_obs:0.6g}"]
                    + [
                        "{}={:0.3g}".format(
                            "".join(p[0] for p in k.split("_")).upper(), v
                        )
                        for k, v in median.items()
                        if v.numel() == 1
                    ]
                )
            )
        if check_loss and step >= 50:
            prev = torch.tensor(losses[-50:-25], device="cpu").median().item()
            curr = torch.tensor(losses[-25:], device="cpu").median().item()
            assert (curr - prev) < num_obs, "loss is increasing"

    result = predict(guide, dataset, num_samples=num_samples)
    result["losses"] = losses
    series["loss"] = losses
    result["series"] = dict(series)
    result["params"] = {
        k: v.detach().float().cpu().clone()
        for k, v in param_store.items()
        if v.numel() < 1e7
    }
    result["walltime"] = default_timer() - start_time
    return result


def fit_bootstrap(
    dataset,
    *,
    model_type,
    guide_type,
    learning_rate=0.05,
    learning_rate_decay=0.1,
    num_steps=3001,
    num_samples=100,
    clip_norm=10.0,
    rank=10,
    jit=True,
    log_every=None,
    seed=20210319,
    check_loss=False,
):
    start_time = default_timer()
    logger.info(f"Fitting {guide_type} guide via bootstrap")
    if log_every is None:
        log_every = num_steps - 1

    # Block bootstrap over places.
    T, P, S = dataset["weekly_strains"].shape
    weight_dist = dist.Multinomial(total_count=P, probs=torch.full((P,), 1 / P))
    weighted_dataset = dataset.copy()

    moments = defaultdict(WelfordCovariance)
    for step in range(num_samples):
        pyro.set_rng_seed(seed + step)
        weights = weight_dist.sample()[:, None]
        weighted_dataset["weekly_strains"] = weights * dataset["weekly_strains"]

        median = fit_svi(
            weighted_dataset,
            model_type=model_type,
            guide_type=guide_type,
            learning_rate=learning_rate,
            learning_rate_decay=learning_rate_decay,
            num_steps=num_steps,
            num_samples=1,
            clip_norm=clip_norm,
            rank=rank,
            log_every=log_every,
            seed=seed + step,
        )["median"]
        for k, v in median.items():
            moments[k].update(v.cpu())

        del median
        pyro.clear_param_store()
        gc.collect()

    result = {}
    result["mean"] = {k: v._mean for k, v in moments.items()}
    result["std"] = {k: v.get_covariance(regularize=False) for k, v in moments.items()}
    result["walltime"] = default_timer() - start_time
    return result


def log_stats(dataset, result):
    mutations = dataset["mutations"]
    mean = result["mean"]["rate_coef"].cpu()
    if not mean.shape:
        return  # Work around error in map estimation.
    std = result["std"]["rate_coef"].cpu()
    sig = mean.abs() / std
    logger.info(f"|μ|/σ [median,max] = [{sig.median():0.3g},{sig.max():0.3g}]")
    for m in ["S:D614G", "S:N501Y", "S:E484K", "S:L452R"]:
        i = mutations.index(m)
        logger.info("ΔlogR({}) = {:0.3g} ± {:0.2f}".format(m, mean[i], std[i]))

    # Compute posterior predictive error.
    true = dataset["weekly_strains"] + 1 / dataset["weekly_strains"].shape[-1]
    true /= true.sum(-1, True)
    pred = result["median"]["probs"]
    error = (true - pred).abs()
    mae = error.mean(0)
    mse = error.square().mean(0)
    logger.info("MAE = {:0.4g}, RMSE = {:0.4g}".format(mae.mean(), mse.mean().sqrt()))
    queries = {
        "England": ["B.1.1.7"],
        # "England": ["B.1.1.7", "B.1.177", "B.1.1", "B.1"],
        # "USA / California": ["B.1.1.7", "B.1.429", "B.1.427", "B.1.2", "B.1", "P.1"],
    }
    for place, strains in queries.items():
        matches = [p for name, p in dataset["location_id"].items() if place in name]
        assert len(matches) == 1, matches
        p = matches[0]
        logger.info(
            "{}\tMAE = {:0.3g}, RMSE = {:0.3g}".format(
                place, mae[p].mean(), mse[p].mean().sqrt()
            )
        )
        for strain in strains:
            s = dataset["lineage_id"][strain]
            logger.info(
                "{} {}\tMAE = {:0.3g}, RMSE = {:0.3g}".format(
                    place, strain, mae[p, s], mse[p, s].sqrt()
                )
            )


def log_holdout_stats(fits):
    assert len(fits) > 1
    fits = list(fits.items())
    for i, (name1, fit1) in enumerate(fits[:-1]):
        for name2, fit2 in fits[i + 1 :]:
            # Compute mutation correlation.
            mutations = sorted(set(fit1["mutations"]) & set(fit2["mutations"]))
            means = []
            for fit in (fit1, fit2):
                mutation_id = {m: i for i, m in enumerate(fit["mutations"])}
                idx = torch.tensor([mutation_id[m] for m in mutations])
                means.append(fit["median"]["rate_coef"][idx])
            mutation_correlation = pearson_correlation(means[0], means[1]).item()

            # Compute lineage correlation.
            means = []
            for fit in (fit1, fit2):
                rate = fit["median"]["rate"]
                if rate.dim() == 2:
                    rate = rate.median(0).values
                means.append(rate)
            lineage_correlation = pearson_correlation(means[0], means[1]).item()
            logger.info(
                f"ρ_mutation = {mutation_correlation:0.3g}, "
                f"ρ_lineage = {lineage_correlation:0.3g} "
                f"for {name1} vs {name2}"
            )
