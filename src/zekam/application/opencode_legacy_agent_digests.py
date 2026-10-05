"""Gecmis Zekam OpenCode ajan sablonlarinin exact sha256 digest'leri (yalniz sahiplik kaniti).

Kaynak: `opencode_agent_bootstrap.py` git gecmisindeki gozden gecirilmis surumler. Digest
taniyan dosya Zekam-owned sayilir; farkli icerik conflict kalir.
"""

from __future__ import annotations

from typing import Final

LEGACY_AGENT_TEMPLATE_DIGESTS: Final[dict[str, frozenset[str]]] = {
    "zekam-builder.md": frozenset(
        (
            "sha256:0e32f486298122ac6341bbef11e9174a56bfef79609eff2e413c2251d89b10bf",
            "sha256:20e9faeb6642f44a5585fe3b9ea35ee3d906e78a3e038ef2828cea4189873506",
            "sha256:6c7e3559dfbfa3977b4e1cd3829b68f355ae40c07a7f5b333b4667ac2502cc3f",
            "sha256:7cd053237f9eb5d4aa9d0428e718452ca7c154f445b12b1e33b6e61d0af5a95a",
            "sha256:a8a3abdbd4437ac40f5877beb13a3b6b28a8a794eee7d2175ce943d54d75e296",
            "sha256:e495989e16933fe993533d67674a7854bec5219a16f914bfe40c193c33339aa4",
        )
    ),
    "zekam-coordinator.md": frozenset(
        (
            "sha256:014870277d62aa0a8c5032651d2ee7c5385c70a12465bb635887d535bc2f0385",
            "sha256:08066939b73747869242b9ce263d65b474ff7b661309c7fd92dbcbbf1aebb77f",
            "sha256:256a027cc6a9b050e3b58fb7d4fbfd688075b1d6d78b3ec1ee5d637dda797436",
            "sha256:2a41190aaf3dc9c43faa1d55a9fdb05e531f31defb9f11a093ac8b6517780ce7",
            "sha256:4919abeeea73e6a041c42645cdd4d9fd8252195e508e4770a173a590dee73e78",
            "sha256:58796c029efe75c2421d15675b4ea5e41d88853178d8c10b366a2fb896b087d9",
            "sha256:5cd689e1bfa0cd1cbc9087459a690dcf75d3c66ecbfda303dc7a0d16e4c2f97a",
            "sha256:619b6963ba0aab15d506a6560a86bda9bd29e68a3bf04bbb2c30487fa027ce3f",
            "sha256:72c685e8b3df491b6a93480110329d7326e790e5fa2a4a523ae2aef8498507a0",
            "sha256:930d0d26f9ae6c9d363180c9679bf762cd4b79cd920fc62adff3b3861cb7e855",
            "sha256:93f88be033fe2b5cca71b69e2b2b49017343a13597e6ee93249524a3ef8f3540",
            "sha256:97987d37ea36f78645aa05146b24ab9a5af30830766ec719f8ddd72f72584b7e",
            "sha256:9ae7c35640e6fe7be7000391486b785a29671719279188903002dd8d2631ee69",
            "sha256:a0f2764312b72f8850500cbcf32db8a0e232bc793974da59a80976ffb3816439",
            "sha256:a3905f3a60ff989d7c68b5f87d4de407427be26dca14d0d9af76ce2db6d299ce",
            "sha256:d0653337b679d35e62657756de291c4e92179bf664ee340200c245419b2210cd",
            "sha256:e4a7977675cfa8784ce93c50f4e0a42c34b0396df0351de40fecbc680002bb37",
            "sha256:f64e1f74dd44a624ef68b4e3c64214dcd041f2a46be6b1b39847c57eb81a175e",
        )
    ),
    "zekam-implementer-224d14fe-d5d1-45aa-8430-392621c555fb.md": frozenset(
        (
            "sha256:27096ac1c1498b2be51efee95fba34cff00c174c14f811520ffb3109af045b3e",
            "sha256:4000436266ee48bf826884babe47d6891b1f2e95935046637249d838bb44a9b0",
            "sha256:5cfcf4a78304dc12bd8b4e995139827c19a4ccd9ff391ad9fb8d95bf042b7c5f",
            "sha256:aa47e8d4e1797cdf005c70e356a38013fae8d5b679d0396ca4f4c1eacc8792c6",
            "sha256:e824070746e0953103afffe880bd4454d22a696a707088f3c000bd7eddd18097",
            "sha256:ecdf4b3d688d0c42626b21fa6e27327e90e088d33a690f443ef9f5b5e21cdeb9",
        )
    ),
    "zekam-implementer-2d13d348-ab24-4738-a19c-6b4be323f836.md": frozenset(
        (
            "sha256:386647c3cb551212a2066bdca5939dfac82683971c5e399e82973118199afe96",
            "sha256:52fe8e2a8b4bf21384844cdcba4bc2b17a525c417ef0845d437900117e76cc59",
            "sha256:68352ac716eeeb7e06980eaf2802dd5da1472c56883d6ad711e2b4246c786caf",
            "sha256:69aaef3eb8f55b7330b774d088b6a41338067bb5bfc2ef7b0edd7eb8efec64c9",
            "sha256:9ec37b510dd167d7ae5630536b612fcbbeece42707eb64a4cb2c0159bfc34c2a",
            "sha256:c82f5cbb4156fa15d95f2cf572aca36249bde1c5de5864dd44921133246028ac",
        )
    ),
    "zekam-implementer-5e62acda-c5a7-4ea7-8f2a-cab9b8d7bb0f.md": frozenset(
        (
            "sha256:2740789f3f6e21622ac5531e19114a3afa32d38fb429b7b8e0897ca3527be6d5",
            "sha256:360534c83649f42b2f72d013faa92fc1227903f4a795512dfe9348f6d0da85a7",
            "sha256:3cc473c25ec519900fd12f9bec7f85714e46ccf5a8b086ef17bd78939d320d58",
            "sha256:525eb8e91ce87ef5223a0214358bf784778f0ba0723a9b46e18b7f8fb1c42ffe",
            "sha256:7ae1a04b44feadaf3ac1f676e8f7d7a4d9ba0dfbdc0eb54458e9e2b9bd19498d",
            "sha256:82cfc1415a63a0f2526d66d22900d846f3421109c0a474079fd4b46b806bb0ff",
        )
    ),
    "zekam-implementer-f7e99dcb-88d6-4c05-8d51-f6f6c8e447f4.md": frozenset(
        (
            "sha256:347be8c9817407587652cbb3de4e13eb7586afea0f7df8eda8f76eca1f1d10ce",
            "sha256:561d93fa548f67d81d8f62e57dac291528ff89fc5087c4b13f219abc3c45f2ce",
            "sha256:642ab1c1fa76692eadd28cdc8966d16a4ad20b8148321f418f184fbcc6ca5828",
            "sha256:652268ca41da3e10522185457d8b979dc83a30b76782b53827ac458d678b40e4",
            "sha256:f002f040c7422b3a82afcabf1dd89c381c83d629f721ad6d017b79bdba4bd9ac",
            "sha256:fa23d75f956fbd57cd17cc67ef52fe0ff51cfbb70fb8395d4211c4658c598ed6",
        )
    ),
    "zekam-memory-curator.md": frozenset(
        (
            "sha256:1ee8e2d653765b99cda5239ec4975aadfc1a0b38522f493a59510c14a6e8671d",
            "sha256:476561ab203c482a1f37312383276296fa7a976b15b90bbe298616557eeec783",
            "sha256:cdc957d93220bb7899e44d0c8633324960e6a8b496fa4abfb7c06006c7e57e04",
            "sha256:db3eeb800fb8206b385c3e01165f768508c54198ad15169b7c6f6634feffc871",
        )
    ),
    "zekam-research-runner.md": frozenset(
        (
            "sha256:0cfcb75577bfd5a0c5025c0ba2d83eff929f667364d446014dc2c8ccdc642e64",
            "sha256:488e62985e4683b67256383fa37716048fb670c4936351736858efef8b663911",
            "sha256:732d95281e708ef776fc4e9b7683c1bb5ba3859fc6734837595956bb6fcc306c",
            "sha256:ea04af3782c69b4e71d2bd546524ad9e1c18c3e169497ca3008013a3a9943894",
        )
    ),
    "zekam-researcher-224d14fe-d5d1-45aa-8430-392621c555fb.md": frozenset(
        (
            "sha256:132277a342516b41d846219ebfa8035775454ca89ff64801505d6186c2ed452c",
            "sha256:310625b4ef1c55bbeda0f34a8e1977d684f3ee48a1c0e25ca5007df332dfcb0e",
            "sha256:6ac394866f8d4ca5ae1b5dfe3d75bb2955e256cb13e53fd70421e1bdad64822c",
            "sha256:99217af986c107e7e780825c6d4062f80c825b51c1c338ab114ab2b5609d6a6d",
            "sha256:99dbc129a31f371d3bb328a20a316d90f28adc6ca175dcc48cae9fd4200372ef",
            "sha256:b3372d321bceb7c2077dd6dcadf7da76848073c8e849f6e2418192d829e487b2",
        )
    ),
    "zekam-researcher-2d13d348-ab24-4738-a19c-6b4be323f836.md": frozenset(
        (
            "sha256:1b6697293168b3fb1b3a89ca8ec180ba15dd3b9a45b40e5fc656e71c2b07c8b3",
            "sha256:2828e4dacd33c321d083b5b0b4e06e8efb55592fac60e7630692d09c0a15e394",
            "sha256:5e0f1f09470e67f7cf9203c664cc241a1c5e8f43187f4d7e35936bb7c185161d",
            "sha256:6ed6d78ffba37c824700a96f8e306bf34ea0fa1e37f2ed6d75dce641c7048ce6",
            "sha256:bb1fbc1e4c341560da60c365e567a8760d4abf52ef2e4884debe72e9fa67084c",
            "sha256:c67f6743c050ba2b5c15f664ba6fdc4b5f62d6968405b5fd065f24774c04cb21",
        )
    ),
    "zekam-researcher-5e62acda-c5a7-4ea7-8f2a-cab9b8d7bb0f.md": frozenset(
        (
            "sha256:3ac60266bdbce6da1c7abf1a27377aa5002664c941e2af7c473cef9d10aedbfa",
            "sha256:511af69eff2107698848d280036f7db5f9f9e43b4e9f5b83a6f38534f593e739",
            "sha256:918e955ea40a2de9860a9cfd93d00d48402e8e0da9f6a569897acec6d7b67f85",
            "sha256:ab76bc7b7bb20786f876e38928701aa6304c7bd8d4a8aead883422ec8bcb2046",
            "sha256:d330f8da7b41257d7e4df8dcdf95de7349a60b4ca9eb8945eaa95859356c49aa",
            "sha256:ed92716ccdd388ecce8a54689731b89d36c37b2f261077d4144e55bb1ce48539",
        )
    ),
    "zekam-researcher-f7e99dcb-88d6-4c05-8d51-f6f6c8e447f4.md": frozenset(
        (
            "sha256:1021696d76a356c119875bd30618f683ed98ed7e2d67b52d8549cb46c524c133",
            "sha256:673b9721edca01937ef7488fac64142b26c7f1c43e5fa57180bda5ec83c75986",
            "sha256:801fba84abdcf098faf9385713e094af34afddc34ea477d1029b7abda3d18239",
            "sha256:c000611072f81021fb4223e6381d90994fa6cf0fd824b136ca70508e0dbe68a4",
            "sha256:c55a0138f59302c62fa09021fc882c6c38d42ddfdd01ac8fc89d1918df4eeef2",
            "sha256:dd7896e59b07ed5b78e54df27288c1543f54ff3d3f046b93cd2b81c9a49b772a",
        )
    ),
    "zekam-researcher.md": frozenset(
        (
            "sha256:08a62645a0c28177fbefd3fe3c6a70a105f951ce1e930701a4c0f181c8fb8e00",
            "sha256:5ae87a3c7f1139d40ce3f14e362ed0603808751ccca03c56e1739a7e91d2f6fa",
            "sha256:a7e90b7aad4125a9ce597cf63cf90494ce0b51a4834d462a8c568647d380a78b",
            "sha256:b15cfe337597c5d94b3fa9223c30423ec47edc1a768e8da876ad65f6d9c34e9e",
            "sha256:d086bf9aa78fc9fffc02f4910b78e01b525db5a27059752801f5e13d36cd7531",
            "sha256:e7e850e46250c4c0c8517955ca7f7c3836048d8b26eb5e6e2f277548fe0d37bb",
        )
    ),
    "zekam-reviewer-224d14fe-d5d1-45aa-8430-392621c555fb.md": frozenset(
        (
            "sha256:2b67e74ab6676729cee27cd4a134c9d196986dd917e2273f16dba4b8b10757a2",
            "sha256:3ee53e1296cf4f6ccc3f1565c4191aa414a568b1e2340a558fbbdad883de10bf",
            "sha256:77c09e4e1d625160145057beca5867a2a8a9d7328b377c84cf074f50f060ff89",
            "sha256:8c8380cfc296577d4e4e1a54c348f6bf3a1aed810d585296e373c6de1296d27e",
            "sha256:bf28463a4571649609a62b20a1fc2b04e8a14dce0b29f8b0214214d504aff0fb",
            "sha256:f9fce38b4a955b90c9d675c268b8da71794b345dd5055133436abdd8f40a2eb1",
        )
    ),
    "zekam-reviewer-2d13d348-ab24-4738-a19c-6b4be323f836.md": frozenset(
        (
            "sha256:009a5195e40e4d1f86ca50733469f5cbf68272245393dc726c64ddcd6ca0176a",
            "sha256:213f0b164875c32b0769ae36ebe11b739263e56afd4e10042eb68aee7c98d57f",
            "sha256:3fb9e05c1232ea5900d42907ff31d99f60fad78b05cc23e59ce54f6444a5f2d0",
            "sha256:3ff32bc09ed90640820dfffbf0ca93a7807cda01a2b332d35c033b58a2852989",
            "sha256:735d8e6786b9fc331bd7fd3ce490df1ec3e136f3e3ec94862ba1a4a8da1ec2b3",
            "sha256:df6ee6caa0da89d8a25130938585735c136db6ea0f882bec715c1344e3a880da",
        )
    ),
    "zekam-reviewer-5e62acda-c5a7-4ea7-8f2a-cab9b8d7bb0f.md": frozenset(
        (
            "sha256:19d1f4ddf7366e0fe689ff6ead2feff68d8927881b323d17aef3da081944f02f",
            "sha256:51edf979211b155ee4f1ecc7b7b28ddb9acd932ebd9ec69463883e6ace32033d",
            "sha256:57c7e766c89f798333c0d584bb843a6c166fdc1cac5a7f9d84d938ab078d649f",
            "sha256:7725f68c9e2ed0dadc362b0387e75e7bc6de30293770834a66c27f4c9fd72e3a",
            "sha256:c20d7193db7ba318c2b8365bd23b4de3a0d11ebcb4908f7f8a10a1fcd23f992d",
            "sha256:d6900eaa6ef7b16c08a25c804cdb42e3390fc411dd500c7bad783c30c566eba1",
        )
    ),
    "zekam-reviewer-f7e99dcb-88d6-4c05-8d51-f6f6c8e447f4.md": frozenset(
        (
            "sha256:0ebc87f327c621656cd3e3fb961c690a51b837d044ba696b54a1c199e86ee9a8",
            "sha256:108b5d4ad109cba6233ceb564af45f2137b97d3511077b258355550769516377",
            "sha256:169c92dc7350b1e9b080bba0d2e3bfc2b2943f5dfb112e079c0d15ce3b30a15e",
            "sha256:4161f592146e68204dac61335f871fb8bf7d4e25bbbadc83ca206617eccfd886",
            "sha256:6a4977f52b484d6d375bc911e3cba2a3ab0597137cd204974c8916f1ae6623f6",
            "sha256:a3033d04c5d5b875a0ebe9ce11f34b79e511a7ef48eba14d3f35fe69270459e0",
        )
    ),
    "zekam-router.md": frozenset(
        (
            "sha256:19d0148710c9d97fa8a53d93974bfa3ac8c7119cf5d95d4e9101526513f00084",
            "sha256:1b3141d4d0c17fc1885a5b088b4fd4b21c6704cc610359c6220bb45b56d20ba4",
            "sha256:bf3a64c57541c73ace24e2eea4841b13fa3e2aace474732fe17def719e55dbfb",
            "sha256:cf7ab55f07f60c286011ca31640b989fdb6341a03dc780947876385da524730d",
            "sha256:d050305e3465857c4c1f5c8664f9961f48e3e7a75911ede0c677b1a1de15dece",
        )
    ),
    "zekam-verifier-224d14fe-d5d1-45aa-8430-392621c555fb.md": frozenset(
        (
            "sha256:2b67e74ab6676729cee27cd4a134c9d196986dd917e2273f16dba4b8b10757a2",
            "sha256:3ee53e1296cf4f6ccc3f1565c4191aa414a568b1e2340a558fbbdad883de10bf",
            "sha256:77c09e4e1d625160145057beca5867a2a8a9d7328b377c84cf074f50f060ff89",
            "sha256:8c8380cfc296577d4e4e1a54c348f6bf3a1aed810d585296e373c6de1296d27e",
            "sha256:bf28463a4571649609a62b20a1fc2b04e8a14dce0b29f8b0214214d504aff0fb",
            "sha256:f9fce38b4a955b90c9d675c268b8da71794b345dd5055133436abdd8f40a2eb1",
        )
    ),
    "zekam-verifier-2d13d348-ab24-4738-a19c-6b4be323f836.md": frozenset(
        (
            "sha256:009a5195e40e4d1f86ca50733469f5cbf68272245393dc726c64ddcd6ca0176a",
            "sha256:213f0b164875c32b0769ae36ebe11b739263e56afd4e10042eb68aee7c98d57f",
            "sha256:3fb9e05c1232ea5900d42907ff31d99f60fad78b05cc23e59ce54f6444a5f2d0",
            "sha256:3ff32bc09ed90640820dfffbf0ca93a7807cda01a2b332d35c033b58a2852989",
            "sha256:735d8e6786b9fc331bd7fd3ce490df1ec3e136f3e3ec94862ba1a4a8da1ec2b3",
            "sha256:df6ee6caa0da89d8a25130938585735c136db6ea0f882bec715c1344e3a880da",
        )
    ),
    "zekam-verifier-5e62acda-c5a7-4ea7-8f2a-cab9b8d7bb0f.md": frozenset(
        (
            "sha256:19d1f4ddf7366e0fe689ff6ead2feff68d8927881b323d17aef3da081944f02f",
            "sha256:51edf979211b155ee4f1ecc7b7b28ddb9acd932ebd9ec69463883e6ace32033d",
            "sha256:57c7e766c89f798333c0d584bb843a6c166fdc1cac5a7f9d84d938ab078d649f",
            "sha256:7725f68c9e2ed0dadc362b0387e75e7bc6de30293770834a66c27f4c9fd72e3a",
            "sha256:c20d7193db7ba318c2b8365bd23b4de3a0d11ebcb4908f7f8a10a1fcd23f992d",
            "sha256:d6900eaa6ef7b16c08a25c804cdb42e3390fc411dd500c7bad783c30c566eba1",
        )
    ),
    "zekam-verifier-f7e99dcb-88d6-4c05-8d51-f6f6c8e447f4.md": frozenset(
        (
            "sha256:0ebc87f327c621656cd3e3fb961c690a51b837d044ba696b54a1c199e86ee9a8",
            "sha256:108b5d4ad109cba6233ceb564af45f2137b97d3511077b258355550769516377",
            "sha256:169c92dc7350b1e9b080bba0d2e3bfc2b2943f5dfb112e079c0d15ce3b30a15e",
            "sha256:4161f592146e68204dac61335f871fb8bf7d4e25bbbadc83ca206617eccfd886",
            "sha256:6a4977f52b484d6d375bc911e3cba2a3ab0597137cd204974c8916f1ae6623f6",
            "sha256:a3033d04c5d5b875a0ebe9ce11f34b79e511a7ef48eba14d3f35fe69270459e0",
        )
    ),
    "zekam-verifier.md": frozenset(
        (
            "sha256:11251d5e5f9c85d389f5f8d8b24965b935ddb395ef108bd57b50868506da31da",
            "sha256:259675b36928c4b5235e11a5885c19b2e5aaf38f693e666b7a644bab190bcfea",
            "sha256:26603659bc2a2419f044e932e7cd423901d9a3ad1da06c8c6c398aaba707162e",
            "sha256:72a069a1505c4dd85f8014b9b806928bd1b3ad85690eddb6d319133c32513bc4",
            "sha256:b4a616bdc757251285f12fb377210a44a93c29dca00a046cda4f5d3c2776852e",
            "sha256:d18a2fed10b732e87613aec36ea6dcf2372f8255aa0732f7239b52c0c5f254f0",
        )
    ),
}
