from gnomad_hail import *
from gnomad_hail.resources.variant_qc import get_ucsc_mappability

def import_cds_from_gtf() -> hl.Table:
    """
    Creates a HT with a row for each base / gene that is in a CDS in gencode v19
    :return: HT
    :rtype: Table
    """
    gtf = hl.experimental.import_gtf(
        'gs://hail-common/references/gencode/gencode.v19.annotation.gtf.bgz',
        reference_genome='GRCh37',
        skip_invalid_contigs=True, min_partitions=200
    )
    gtf = gtf.filter((gtf.feature == 'CDS') & (gtf.transcript_type == 'protein_coding') & (gtf.tag == 'basic'))
    gtf = gtf.annotate(locus=hl.range(gtf.interval.start.position, gtf.interval.end.position).map(lambda x: hl.locus(gtf.interval.start.contig, x, 'GRCh37')))
    gtf = gtf.key_by().select('gene_id', 'locus', 'gene_name').explode('locus')
    gtf = gtf.key_by('locus', 'gene_id').distinct()
    return gtf.checkpoint('gs://gnomad-tmp/gencode_grch37.gene_by_base.ht', overwrite=True)

def compute_per_base_cds_coverage():
    """
    Creates a HT with a row for each base and gene that is in a CDS in gencode v19 with the following information:
    1) gene id
    2) gnomAD exomes coverage by platform
    3) gnomAD genomes coverage by PCR status
    """

    def get_haploid_coverage_struct(mt):
        return hl.cond(
            mt.locus.in_autosome_or_par(),
            hl.struct(
                mean=0.5*hl.agg.sum(mt.mean  * mt.n)/hl.agg.sum(mt.n),
                median=0.5*hl.median(hl.agg.collect(mt.median)),
                **{
                    f'over_{i/2}': hl.agg.sum(mt[f'over_{float(i)}']) / hl.agg.sum(mt.n)
                    for i in [1] + list(range(5,31,5)) +  [50, 100]
                }
            ),
            hl.struct(
                mean=hl.agg.sum(hl.cond(mt.sex == 'male', mt.mean * mt.n, 0.5*mt.mean * mt.n))/hl.agg.sum(mt.n),
                median=hl.median(hl.agg.collect(hl.cond(mt.sex == 'male', mt.median, 0.5*mt.median))),
                **{
                    f'over_{i/2}':
                    hl.null(hl.tfloat32) if not f'over_{i/2}' in mt.entry else
                    hl.agg.sum(
                        hl.cond(
                            mt.sex == 'male',
                            mt[f'over_{i/2}'],
                            mt[f'over_{float(i)}']
                        )
                    ) / hl.agg.sum(mt.n)
                    for i in [1] + list(range(5,31,5)) +  [50, 100]
                }
            )
        )

    def get_cov_ht(data_type: str) -> hl.Table:
        cov_mt = hl.read_matrix_table(coverage_mt_path(data_type, grouped=True))
        qc_platforms = cov_mt.aggregate_cols(hl.agg.collect_as_set(cov_mt.qc_platform))
        return cov_mt.select_rows(
            **{
                f'{data_type}_{platform}': hl.agg.filter(
                    cov_mt.qc_platform == platform,
                    get_haploid_coverage_struct(cov_mt)
                ) for platform in qc_platforms
            },
            **{f'{data_type}_all': get_haploid_coverage_struct(cov_mt)}
        ).rows()

    genomes_cov = get_cov_ht('genomes')
    exomes_cov = get_cov_ht('exomes')
    ucsc_mappability = get_ucsc_mappability()
    gtf = import_cds_from_gtf()

    gtf = gtf.annotate(
        **exomes_cov[gtf.locus],
        **genomes_cov[gtf.locus],
        mappability=ucsc_mappability[gtf.locus].duke_35_map
    )
    gtf.write('gs://gnomad-lfran/exomes_genomes_coverage/gencode_grch37.gene_by_base.cov.ht', overwrite=True)


def export_gene_coverage():
    """
    Exports gene coverage summary stats as tsv.
    """
    min_good_coverage_dp = 'over_10.0'
    min_good_coverage_prop = 0.8

    cov = hl.read_table('gs://gnomad-lfran/exomes_genomes_coverage/gencode_grch37.gene_by_base.cov.ht')
    cov = hl.filter_intervals(cov, [hl.parse_locus_interval('Y')], keep=False)

    cov = cov.group_by(cov.gene_id).aggregate(
        gene_name=hl.agg.take(cov.gene_name, 1)[0],
        gene_interval=hl.interval(
            hl.locus(hl.agg.take(cov.locus.contig, 1)[0], hl.agg.min(cov.locus.position)),
            hl.locus(hl.agg.take(cov.locus.contig, 1)[0], hl.agg.max(cov.locus.position))
        ),
        mean_mappability=hl.agg.mean(cov.mappability),
        median_mappability=hl.agg.approx_quantiles(cov.mappability, 0.5),
        cov_stats=[
            hl.struct(
                data_type=x.split("_")[0],
                platform=hl.delimit(x.split("_")[1:], delimiter="_"),
                frac_well_covered_bases=hl.agg.fraction(cov[x][min_good_coverage_dp] >= min_good_coverage_prop),
                mean_well_covered_samples=hl.agg.mean(cov[x][min_good_coverage_dp])
            ) for x in list(cov.row_value) if x.startswith('exomes') or x.startswith('genomes')
        ]
    )

    cov = cov.explode('cov_stats')
    cov = cov.select(
        'gene_name',
        'gene_interval',
        'mean_mappability',
        'median_mappability',
        **cov.cov_stats

    )
    cov = cov.annotate_globals(
        min_good_coverage_prop=min_good_coverage_prop,
        min_good_coverage_dp=min_good_coverage_dp
    )

    cov.export('gs://gnomad-public/papers/2019-flagship-lof/v1.1/summary_gene_coverage/gencode_grch37_gene_by_platform_coverage_summary.tsv.gz')


def main(args):

    if args.compute_per_base_cds_coverage:
        print("Computing per-base CDS coverage")
        compute_per_base_cds_coverage()

    if args.export_gene_coverage:
        print("Exporting gene coverage")
        export_gene_coverage()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--compute_per_base_cds_coverage', help='Computes per-base coverage.', action='store_true')
    parser.add_argument('--export_gene_coverage', help='Exports gene coverage in long format to tsv.', action='store_true')
    args = parser.parse_args()
    main(args)
