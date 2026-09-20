"""Exercise the actual importer methods with an in-memory recorder boundary."""
import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from test_api import api
from test_calculations import calculations

source = ast.parse((Path(__file__).parents[1] / 'custom_components/eon_next_energy/coordinator.py').read_text())
methods = []
for node in source.body:
    if isinstance(node, ast.FunctionDef) and node.name in {'_meter_key', '_statistic_ids'}:
        methods.append(node)
    if isinstance(node, ast.ClassDef) and node.name == 'EonNextCoordinator':
        methods.extend(n for n in node.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_import_meter')
module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *methods], type_ignores=[])
namespace = dict(Decimal=Decimal, datetime=datetime, time=__import__('datetime').time, timedelta=timedelta, UTC=UTC,
    LONDON=calculations.LONDON, sha256=sha256, DOMAIN='eon_next_energy', CONF_HISTORY_DAYS='history_days',
    DEFAULT_HISTORY_DAYS=30, CONF_GAS_CALORIFIC_VALUE='gas_calorific_value', DEFAULT_GAS_CALORIFIC_VALUE=0, replace=__import__('dataclasses').replace, gas_kwh_from_volume=calculations.gas_kwh_from_volume, CORRECTION_LOOKBACK_DAYS=14, aggregate_complete_hours=calculations.aggregate_complete_hours,
    StatisticData=dict, StatisticMetaData=dict, StatisticMeanType=SimpleNamespace(NONE=0),
    UnitOfEnergy=SimpleNamespace(KILO_WATT_HOUR='kWh'), UnitOfVolume=SimpleNamespace(CUBIC_METERS='m³'),
    EnergyConverter=SimpleNamespace(UNIT_CLASS='energy'), VolumeConverter=SimpleNamespace(UNIT_CLASS='volume'),
    MeterImportResult=SimpleNamespace)
exec(compile(ast.fix_missing_locations(module), '<importer>', 'exec'), namespace)

class ImportTests(IsolatedAsyncioTestCase):
    async def check_import(self, unit, fuel, prior=None, cv=0):
        writes=[]
        namespace['async_add_external_statistics']=lambda hass, metadata, data: writes.append((metadata, data))
        start=datetime(2026, 9, 19, 12, tzinfo=UTC)
        readings=[api.EonInterval(start, start+timedelta(minutes=30), Decimal('0.5')),
            api.EonInterval(start+timedelta(minutes=30), start+timedelta(hours=1), Decimal('0.25'))]
        owner=SimpleNamespace(hass=None, _last_statistic=AsyncMock(return_value=(prior, 10.0 if prior else 0.0)),
            _sum_before=AsyncMock(return_value=10.0 if prior else 0.0), _setting=lambda k, d:cv if k == 'gas_calorific_value' else d,
            client=SimpleNamespace(async_get_consumption=AsyncMock(return_value=readings)))
        tariff=calculations.Tariff(*map(Decimal, ['0.069','0.3367','0.60','0.0812','0.2916']))
        result=await namespace['_import_meter'](owner, api.EonMeter('A','M',fuel,unit),1,tariff)
        return owner,result,writes

    async def test_volume_metadata_and_no_cost(self):
        owner,result,writes=await self.check_import('m3','gas')
        self.assertEqual(len(writes),1)
        meta,rows=writes[0]
        self.assertEqual((meta['unit_class'],meta['unit_of_measurement']),('volume','m³'))
        self.assertTrue(meta['statistic_id'].endswith('_volume'))
        self.assertEqual(rows[0]['sum'],0.75)
        self.assertIsNone(result.cost_statistic_id)
        self.assertEqual(owner._last_statistic.await_count,1)

    async def test_volume_resume_uses_rolling_window_without_cost_series(self):
        prior=datetime(2026,9,18,12,tzinfo=UTC)
        owner,result,writes=await self.check_import('m³','gas',prior)
        requested=owner.client.async_get_consumption.call_args.args[1]
        self.assertEqual(requested.astimezone(calculations.LONDON).date(), (prior-timedelta(days=14)).date())
        self.assertEqual(writes[0][1][0]['sum'],10.75)

    async def test_electricity_retains_cost_and_statistic_id(self):
        owner,result,writes=await self.check_import('kWh','electricity')
        self.assertEqual(len(writes),2)
        self.assertEqual(writes[0][0]['unit_of_measurement'],'kWh')
        self.assertTrue(writes[0][0]['statistic_id'].endswith('_consumption'))
        self.assertEqual(writes[1][0]['unit_of_measurement'],'GBP')
        self.assertAlmostEqual(writes[1][1][0]['sum'],0.852525)

    async def test_estimated_gas_uses_separate_kwh_and_cost_series(self):
        owner,result,writes=await self.check_import('m3','gas',cv=39.5)
        self.assertEqual(len(writes),2)
        self.assertEqual(writes[0][0]['unit_of_measurement'],'kWh')
        self.assertIn('_estimated_consumption',writes[0][0]['statistic_id'])
        self.assertAlmostEqual(writes[0][1][0]['sum'],8.415475)
        self.assertAlmostEqual(writes[1][1][0]['sum'],8.415475*0.0812+0.2916)
