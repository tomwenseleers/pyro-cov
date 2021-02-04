import pytest

from pyrophylo.strains import TimeSpaceStrainModel, simulate


@pytest.mark.parametrize("T", [32])
@pytest.mark.parametrize("R", [6])
@pytest.mark.parametrize("S", [5])
def test_strains(T, R, S):
    dataset = simulate(T, R, S)
    model = TimeSpaceStrainModel(**dataset)
    model.fit(num_steps=101)
    model.median()