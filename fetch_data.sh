# We are not touching ama assn license is... bad. very very bad.
# Instead download public (semi-complete) CPT code lists
# The fact that you have to PAY FOR A FUCKING LICENSE to READ YOUR OWN
# fucking health insurance denials should be fucking criminal.
# buuuut... welll..... America fuck yeah!
# And redistributing these (publicly available) copies could be tricky
# since we don't have a license for them, but I think we can download them
# on each node. Downside: they might go away and then shit will break.
cd data
mkdir fetched
wget https://gist.githubusercontent.com/lieldulev/439793dc3c5a6613b661c33d71fdd185/raw/25c3abcc5c24e640a0a5da1ee04198a824bf58fa/cpt4.csv
wget https://www.cdc.gov/nhsn/xls/cpt-pcm-nhsn.xlsx
