import sys

from reports.daily import build

if __name__ == "__main__":
    print(build(int(sys.argv[1]), sys.argv[2]))
