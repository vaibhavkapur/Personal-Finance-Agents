import argparse
import os
from .workflows.service import PaydayService

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Seed a new mock-only payday workspace without overwriting existing records.')
    parser.add_argument('--data-dir',default=os.getenv('PAYDAY_DATA_DIR','data'))
    parser.add_argument('--scenario',choices=['supported','late'],default='supported')
    args=parser.parse_args()
    PaydayService(args.data_dir).seed(scenario=args.scenario)
    print('Seeded synthetic workspace in',args.data_dir)
