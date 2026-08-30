import csv
import os
from django.core.management.base import BaseCommand
from django.db import transaction
from django.conf import settings
from business_intel.models import AdminUnit, PopulationEstimate

class Command(BaseCommand):
    help = 'Load 2019 Kenya census data from CSV files into AdminUnit and PopulationEstimate.'

    CENSUS_YEAR = 2019

    def add_arguments(self, parser):
        parser.add_argument(
            '--path',
            type=str,
            default=os.path.join(settings.BASE_DIR, 'business_intel', 'data', 'census_2019', 'by_volume'),
            help='Root directory of the census CSV files',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        base = options['path']

        # 1. Read county GPS to get codes and centroids
        county_info = self.load_county_gps(os.path.join(base, 'other', 'CountyGPS.csv'))

        # 2. Load counties
        self.load_counties(os.path.join(base, 'V1', 'V1_T2.2.csv'), county_info)

        # 3. Load sub-county codes from V2_T1.2.csv
        subcode_map = self.load_subcounty_codes(os.path.join(base, 'V2', 'V2_T1.2.csv'), county_info)

        # 4. Load sub-county data
        self.load_subcounties(os.path.join(base, 'V1', 'V1_T2.5.csv'), subcode_map)

        # 5. Load household & land area (optional)
        self.load_household_land(base, county_info, subcode_map)

        self.stdout.write(self.style.SUCCESS('Census data loaded successfully.'))

    def load_county_gps(self, filepath):
        """Read CountyGPS.csv -> {county_name: {'code': code, 'lat': lat, 'lng': lng}}"""
        data = {}
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row['County'].strip().upper()
                data[name] = {
                    'code': row['CountyCode'].strip(),
                    'lat': float(row['Latitude']),
                    'lng': float(row['Longitude']),
                }
        return data

    def load_counties(self, filepath, county_info):
        """Populate AdminUnit and PopulationEstimate for counties."""
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row['County'].strip().upper()
                if name == 'TOTAL':
                    continue
                info = county_info.get(name)
                if not info:
                    self.stderr.write(f'CountyGPS missing for: {name}')
                    continue
                admin, _ = AdminUnit.objects.update_or_create(
                    code=info['code'],
                    defaults={
                        'name': name.title(),
                        'admin_type': 'county',
                        'centroid_lat': info['lat'],
                        'centroid_lng': info['lng'],
                    }
                )
                PopulationEstimate.objects.update_or_create(
                    admin_unit=admin,
                    year=self.CENSUS_YEAR,
                    defaults={
                        'total': self._int(row['Total']),
                        'male': self._int(row['Male']),
                        'female': self._int(row['Female']),
                        'intersex': self._int(row['Intersex']),
                    }
                )

    def load_subcounty_codes(self, filepath, county_info):
        """Read V2_T1.2.csv -> mapping from (county_name, sub_name) to sub_code."""
        mapping = {}
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                county_name = row['County'].strip().upper()
                sub_name = row['SubCounty'].strip()
                sub_code = row['SubCountyCode'].strip()
                # Ensure county exists in AdminUnit
                county_code = county_info.get(county_name, {}).get('code')
                if not county_code:
                    continue
                mapping[(county_name, sub_name)] = {
                    'sub_code': sub_code,
                    'county_code': county_code,
                }
        return mapping

    def load_subcounties(self, filepath, subcode_map):
        """Populate AdminUnit and PopulationEstimate for sub-counties."""
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                county_name = row['County'].strip().upper()
                sub_name = row['SubCounty'].strip()
                if county_name == 'XXX' and sub_name == 'Total':
                    continue
                if 'Special Area' in sub_name or '*' in sub_name:
                    continue
                key = (county_name, sub_name)
                map_entry = subcode_map.get(key)
                if not map_entry:
                    self.stderr.write(f'Sub-county code missing for: {county_name} - {sub_name}')
                    continue
                county_unit = AdminUnit.objects.get(code=map_entry['county_code'])
                admin, _ = AdminUnit.objects.update_or_create(
                    code=map_entry['sub_code'],
                    defaults={
                        'name': sub_name,
                        'admin_type': 'sub_county',
                        'parent': county_unit,
                    }
                )
                PopulationEstimate.objects.update_or_create(
                    admin_unit=admin,
                    year=self.CENSUS_YEAR,
                    defaults={
                        'total': self._int(row['Total']),
                        'male': self._int(row['Male']),
                        'female': self._int(row['Female']),
                        'intersex': self._int(row['Intersex']),
                    }
                )

    def load_household_land(self, base, county_info, subcode_map):
        """Optionally load households and land area from V1_T2.3 (county) and V1_T2.6 (sub-county)."""
        # County-level
        with open(os.path.join(base, 'V1', 'V1_T2.3.csv'), 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row['County'].strip().upper()
                if name.startswith('KENYA') or name == 'TOTAL':
                    continue
                code = county_info.get(name, {}).get('code')
                if not code:
                    continue
                admin = AdminUnit.objects.get(code=code)
                PopulationEstimate.objects.filter(admin_unit=admin, year=self.CENSUS_YEAR).update(
                    households=self._int(row['NumberOfHouseholds']),
                    avg_household_size=float(row['AverageHouseholdSize']) if row['AverageHouseholdSize'] else None,
                )

        with open(os.path.join(base, 'V1', 'V1_T2.4.csv'), 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row['County'].strip().upper()
                if name.startswith('KENYA') or name == 'TOTAL':
                    continue
                code = county_info.get(name, {}).get('code')
                if not code:
                    continue
                admin = AdminUnit.objects.get(code=code)
                PopulationEstimate.objects.filter(admin_unit=admin, year=self.CENSUS_YEAR).update(
                    land_area_sqkm=float(row['LandArea(in Sq. Km)']) if row['LandArea(in Sq. Km)'] else None,
                    density_per_sqkm=float(row['Population Density(No. per Sq. Km)']) if row['Population Density(No. per Sq. Km)'] else None,
                )

        # Sub-county level
        with open(os.path.join(base, 'V1', 'V1_T2.6.csv'), 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                county_name = row['County'].strip().upper()
                sub_name = row['SubCounty'].strip()
                if county_name == 'XXX' and sub_name == 'Total':
                    continue
                if 'Special Area' in sub_name or '*' in sub_name:
                    continue
                key = (county_name, sub_name)
                map_entry = subcode_map.get(key)
                if not map_entry:
                    continue
                admin = AdminUnit.objects.get(code=map_entry['sub_code'])
                PopulationEstimate.objects.filter(admin_unit=admin, year=self.CENSUS_YEAR).update(
                    households=self._int(row['NumberOfHouseholds']),
                    avg_household_size=float(row['AverageHouseholdSize']) if row['AverageHouseholdSize'] else None,
                    land_area_sqkm=float(row['LandArea(in Sq. Km)']) if row['LandArea(in Sq. Km)'] else None,
                    density_per_sqkm=float(row['Population Density(No. per Sq. Km)']) if row['Population Density(No. per Sq. Km)'] else None,
                )

    @staticmethod
    def _int(value):
        if not value or value.strip() == '':
            return 0
        return int(float(value))