# The orders extract (SYNTHETIC)

## What it is

`data/orders.csv` is a synthetic extract of order lines, one row per order, for 3 to 8 March
2025. It is made up; the numbers describe no real business.

## Columns

`order_id`, `day` (YYYY-MM-DD), `region` (north, south, east or west), `sku` (A-100, B-200 or
C-300), `qty` (units) and `status`.

## What counts as a sale

`status` is `shipped` or `cancelled`. Only `shipped` rows are sales: a cancelled order is not
counted in order counts, units or revenue. The cancelled share of a group is the number of
cancelled rows divided by all rows of the group.
